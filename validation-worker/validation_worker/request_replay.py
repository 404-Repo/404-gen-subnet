"""Answer the local replay miner with recorded witness responses."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import struct
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Self, Unpack, overload

from miner_runtime.accounting import PromptBudget
from miner_runtime.observations import ReplayMismatch
from openai.types.responses import Response, ResponseStreamEvent
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict
from renderer_client import Renderer, RenderOptions
from renderer_client.client import DEFAULT_OPTIONS
from witness_client import AsyncWitnessClient, CapturedResponse, WitnessClient, WitnessError
from witness_client.calls import Digest, RequestKey, canonical
from witness_client.pods import CallView
from witness_client.response import AsyncModelStream, ModelStream, model_error, model_result


PNG_URL = "data:image/png;base64,"
PNG_TOLERANCE_PIXEL_RATIO = 20_000
PNG_TOLERANCE_MAX_CHANNEL_DELTA = 1


def png_metadata(png: bytes) -> bytes:
    offset = 8
    chunks = []
    while offset < len(png):
        length = struct.unpack_from(">I", png, offset)[0]
        if png[offset + 4 : offset + 8] != b"IDAT":
            chunks.append(png[offset : offset + 12 + length])
        offset += 12 + length
    return b"".join(chunks)


def png_pixels(png: bytes) -> str:
    """Identify exact native PNG pixels, dimensions, mode and non-image chunks."""
    with Image.open(io.BytesIO(png)) as verified:
        verified.verify()
    with Image.open(io.BytesIO(png)) as image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise ValueError("PNG matching requires a single PNG image")
        digest = hashlib.sha256(canonical([image.mode, image.size]))
        digest.update(image.tobytes())
    digest.update(png_metadata(png))
    return digest.hexdigest()


def png_request(body: bytes, *, reference: bytes | None = None) -> bytes | None:
    """Fingerprint inline PNGs, or restore an identical frozen reference's encoding."""
    value = json.loads(body)
    reference_pixels = png_pixels(reference) if reference is not None else None
    changed = False

    def visit(item: object) -> None:
        nonlocal changed
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            url = item.get("image_url")
            if item.get("type") == "input_image" and isinstance(url, str) and url.startswith(PNG_URL):
                pixels = png_pixels(base64.b64decode(url[len(PNG_URL) :], validate=True))
                if reference is None:
                    item["image_url"] = {"png_pixels_sha256": pixels}
                    changed = True
                elif pixels == reference_pixels:
                    item["image_url"] = PNG_URL + base64.b64encode(reference).decode("ascii")
                    changed = True
            else:
                for child in item.values():
                    visit(child)

    visit(value)
    return canonical(value) if changed else None


@dataclass(frozen=True)
class PngDifference:
    path: str
    changed_pixels: int
    total_pixels: int
    max_channel_delta: int


def png_request_tolerance(body: bytes, recorded: bytes, reference: bytes | None) -> tuple[PngDifference, ...] | None:
    """Allow at most one RGB step in one of 20,000 pixels per non-reference PNG."""

    def unpack(data: bytes) -> tuple[bytes, list[tuple[str, bytes]]]:
        value = json.loads(data)
        images: list[tuple[str, bytes]] = []

        def visit(item: object, path: str) -> None:
            if isinstance(item, list):
                for index, child in enumerate(item):
                    visit(child, f"{path}[{index}]")
            elif isinstance(item, dict):
                url = item.get("image_url")
                if item.get("type") == "input_image" and isinstance(url, str) and url.startswith(PNG_URL):
                    images.append((path + ".image_url", base64.b64decode(url[len(PNG_URL) :], validate=True)))
                    item["image_url"] = "<inline-png>"
                else:
                    for key, child in item.items():
                        visit(child, f"{path}.{key}")

        visit(value, "$")
        return canonical(value), images

    try:
        actual_fields, actual_images = unpack(body)
        saved_fields, saved_images = unpack(recorded)
        if actual_fields != saved_fields:
            return None
        reference_pixels = png_pixels(reference) if reference is not None else None
        differences = []
        for (path, actual), (_, saved) in zip(actual_images, saved_images, strict=True):
            actual_pixels, saved_pixels = png_pixels(actual), png_pixels(saved)
            if actual_pixels == saved_pixels:
                continue
            if reference_pixels in (actual_pixels, saved_pixels) or png_metadata(actual) != png_metadata(saved):
                return None
            with Image.open(io.BytesIO(actual)) as first, Image.open(io.BytesIO(saved)) as second:
                if first.mode not in ("RGB", "RGBA") or first.mode != second.mode or first.size != second.size:
                    return None
                if first.convert("RGBA").getchannel("A").tobytes() != second.convert("RGBA").getchannel("A").tobytes():
                    return None
                delta = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
                mask = ImageChops.lighter(
                    ImageChops.lighter(delta.getchannel("R"), delta.getchannel("G")), delta.getchannel("B")
                )
                histogram = mask.histogram()
                maximum = max(index for index, count in enumerate(histogram) if count)
                total = first.width * first.height
                changed = total - histogram[0]
                if maximum > PNG_TOLERANCE_MAX_CHANNEL_DELTA or changed * PNG_TOLERANCE_PIXEL_RATIO > total:
                    return None
                differences.append(PngDifference(path, changed, total, maximum))
        return tuple(differences) if differences else None
    except (ValueError, OSError):
        return None


class CachedImageRequest(BaseModel):
    """Cached PNG comparison fingerprints for one recorded request.

    Stores the original request hash and a comparison hash that ignores PNG
    compression while preserving pixels, metadata and the other request fields.
    A None comparison hash means there is no supported inline PNG to compare.
    """

    model_config = ConfigDict(extra="forbid")

    request_sha256: Digest
    image_request_sha256: Digest | None


class ImageRequestIndex(BaseModel):
    """PNG comparison fingerprints for a group of recorded requests.

    Maps original request hashes to their cached comparison hashes, or None
    for requests without a supported inline PNG.
    """

    model_config = ConfigDict(extra="forbid")

    requests: dict[Digest, Digest | None]


class _RecordedStream(ModelStream):
    """Log the miner's reads and closes of a recorded response."""

    def __init__(self, stream: ModelStream, emit: Callable[..., None], call_id: str, occurrence: int) -> None:
        self.stream, self.emit, self.call_id, self.occurrence = stream, emit, call_id, occurrence
        self.index = 0
        self.closed = False

    def __next__(self) -> ResponseStreamEvent:
        index, self.index = self.index, self.index + 1
        self.emit(
            "model_operation", call_id=self.call_id, occurrence=self.occurrence, action="next", index=index, stream=True
        )
        return next(self.stream)

    def close(self) -> None:
        if self.closed:
            return
        self.emit(
            "model_operation",
            call_id=self.call_id,
            occurrence=self.occurrence,
            action="close",
            index=self.index,
            stream=True,
        )
        self.stream.close()
        self.closed = True


class SavedCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call: CallView
    model: str = ""
    usage: dict[str, int] | None = None
    response_sha256: Digest | None = None
    provider_status: int | None = None


class RequestInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["request_inventory"] = "request_inventory"
    version: Literal[1] = 1
    scope_key: str
    pricing_sha256: Digest
    captures: dict[str, SavedCapture]


def replayable_captures(inventory: RequestInventory) -> dict[str, SavedCapture]:
    """Prefer completed responses, otherwise retain an unambiguous recorded failure."""
    issued: dict[str, SavedCapture] = {}
    failed: dict[str, list[SavedCapture]] = {}
    for item in inventory.captures.values():
        if item.call.phase == "issued":
            issued[item.call.request.id] = item
        elif item.call.phase == "failed" and (item.call.manifest_sha256 is not None or item.call.authorized):
            failed.setdefault(item.call.request.id, []).append(item)
    return {**{key: items[0] for key, items in failed.items() if len(items) == 1}, **issued}


class EvidenceUnavailable(ValueError):
    """A requested outcome is unresolved or cannot be reconstructed from retained evidence."""


class ReplayPrompt(BaseModel):
    id: str
    sha256: Digest


class PreparedReplay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    competition_repository: str
    competition_ref: str
    round: str
    hotkey: str
    source_repository: str
    source_commit: str
    storage_prefix: str
    seed: int
    prompts: list[ReplayPrompt]
    providers: list[Literal["api", "codex"]]
    historical_observations_available: Literal[False] = False


class RecordedWitnessClient(WitnessClient):
    """Give the local replay miner the saved answer for each matching request.

    Requests use the witness client's normal format. Responses and errors come
    from recordings; preview renders go to the renderer supplied by the host.
    """

    def __init__(
        self,
        directory: Path,
        emit: Callable[..., None],
        fetch: Callable[[str], tuple[SavedCapture, bytes]],
        *,
        image_index: Callable[[int, str | None], dict[str, str | None]] | None = None,
        reference: bytes | None = None,
        lookup: Callable[[RequestKey], dict[str, SavedCapture]] | None = None,
        request_body: Callable[[RequestKey], bytes] | None = None,
    ) -> None:
        self.emit, self.fetch = emit, fetch
        self.image_index, self.reference = image_index, reference
        self.lookup = lookup
        self.request_body = request_body
        self.looked_up: set[str] = set()
        self.image_aliases: dict[str, RequestKey] = {}
        self.image_indexes: dict[tuple[int, str | None], dict[str, str | None]] = {}
        self.image_tolerance_matches: dict[str, tuple[PngDifference, ...]] = {}
        self.request_matches: dict[str, Literal["exact", "exact_png_pixels", "image_tolerance"]] = {}
        self.inventory = RequestInventory.model_validate_json((directory / "requests.json").read_bytes())
        pricing = (directory / "pricing.json").read_bytes()
        if hashlib.sha256(pricing).hexdigest() != self.inventory.pricing_sha256:
            raise EvidenceUnavailable("Frozen pricing changed after loading")
        self.budget = PromptBudget(pricing)
        self.used: set[str] = set()
        self.unknown_usage: set[str] = set()
        self.assumed_failures: set[str] = set()
        self.problem: Exception | None = None
        self._mode: Literal["api", "codex"] = "api"
        self.responses = self
        self._renderer: Renderer | None = None
        self._lock = threading.Lock()
        self._occurrences: dict[str, int] = {}
        self._provider_selected = False
        self.by_key = replayable_captures(self.inventory)

    @overload
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[False] = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response: ...

    @overload
    def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True],
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> ModelStream: ...

    def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | ModelStream:
        """Answer the miner's request from a matching witness recording."""
        key, body = self._request(request_id, session_id, params)
        call_id = RequestKey.from_body(key.provider, body, key.request_id, session_id=key.session_id).id
        with self._lock:
            occurrence = self._occurrences.get(call_id, 0)
            self._occurrences[call_id] = occurrence + 1
        self.emit("model_operation", call_id=call_id, occurrence=occurrence, action="create", index=0, stream=stream)
        try:
            captured = self._recorded_response(key, body)
        except ReplayMismatch:
            raise
        except WitnessError as error:
            raise model_error(error, self._mode) from error
        result = model_result(captured.status, captured.body, self._mode, stream=stream)
        return _RecordedStream(result, self.emit, call_id, occurrence) if isinstance(result, ModelStream) else result

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        """Ask the replay host for a fresh render of the miner's source."""
        if self._renderer is None:
            raise ReplayMismatch("Execution host did not supply a renderer")
        return self._renderer.render(source, options)

    def close(self) -> None:
        pass

    def resume(self, exchange_id: str) -> Response:
        raise ReplayMismatch("Execution code must use responses.create()")

    def get_submitted(self) -> dict[str, dict[str, object]]:
        raise ReplayMismatch("Submission lookup belongs to the live runner")

    def submit(self, image: bytes, source: str | None) -> dict[str, object]:
        raise ReplayMismatch("Execution code must return its result; the live runner submits it")

    def _png_match(
        self, key: RequestKey, body: bytes, *, allow_tolerance: bool = False
    ) -> tuple[RequestKey, bytes] | None:
        if key.id in self.by_key:
            return None
        matched = self.image_aliases.get(key.id)
        if matched is not None and key.id in self.image_tolerance_matches and not allow_tolerance:
            return None
        if matched is None:
            candidates = {
                item.call.request.id: item.call.request
                for item in self.inventory.captures.values()
                if (item.call.request.provider, item.call.request.request_id, item.call.request.session_id)
                == (key.provider, key.request_id, key.session_id)
            }
            if not candidates:
                return None
            try:
                normalized = png_request(body)
            except (ValueError, OSError):
                return None
            if normalized is None:
                return None
            unavailable = False
            if self.reference is not None:
                restored = png_request(body, reference=self.reference)
                if restored is not None:
                    original = RequestKey.from_body(key.provider, restored, key.request_id, session_id=key.session_id)
                    unavailable = original.id not in self.by_key and any(
                        item.call.request == original
                        and (item.call.authorized or item.call.manifest_sha256 is not None)
                        for item in self.inventory.captures.values()
                    )
            if matched is None and self.image_index is not None:
                group = key.request_id, key.session_id
                if group not in self.image_indexes:
                    self.image_indexes[group] = self.image_index(*group)
                fingerprint = hashlib.sha256(normalized).hexdigest()
                matches = [
                    candidate
                    for call_id, candidate in candidates.items()
                    if call_id in self.by_key and self.image_indexes[group].get(candidate.request_sha256) == fingerprint
                ]
                if len(matches) > 1:
                    raise ReplayMismatch("More than one saved request matches these PNG pixels")
                if matches:
                    matched = matches[0]
            if matched is None and allow_tolerance and not unavailable and self.request_body is not None:
                tolerant = []
                for call_id, candidate in candidates.items():
                    if call_id not in self.by_key:
                        continue
                    differences = png_request_tolerance(body, self.request_body(candidate), self.reference)
                    if differences is not None:
                        tolerant.append((candidate, differences))
                if len(tolerant) > 1:
                    raise ReplayMismatch("More than one saved request matches within image tolerance")
                if tolerant:
                    matched, differences = tolerant[0]
                    self.image_tolerance_matches[key.id] = differences
            if matched is None:
                if unavailable:
                    raise EvidenceUnavailable("Matching call has no usable completed capture")
                return None
            self.image_aliases[key.id] = matched
        differences = self.image_tolerance_matches.get(key.id)
        self.emit(
            "image_tolerance_matched" if differences is not None else "png_request_matched",
            call_id=key.id,
            recorded_call_id=matched.id,
            request_key=key.model_dump(),
            recorded_request_key=matched.model_dump(),
            matching="image_tolerance" if differences is not None else "exact_png_pixels_and_metadata",
            images=[asdict(difference) for difference in differences] if differences is not None else [],
        )
        return matched, body

    def _request(
        self, request_id: int, session_id: str | None, params: ResponseCreateParamsBase
    ) -> tuple[RequestKey, bytes]:
        with self._lock:
            requests = []
            errors = []
            providers: tuple[Literal["api", "codex"], ...] = (
                (self._mode,) if self._provider_selected else ("api", "codex")
            )
            for provider in providers:
                self._mode = provider
                try:
                    requests.append(super()._request(request_id, session_id, params))
                except ValueError as error:
                    errors.append(error)
            if not requests:
                self.emit("model_request_rejected", request_id=request_id, session_id=session_id, error=str(errors[0]))
                raise errors[0]
            try:
                matches = [(key, body) for key, body in requests if key.id in self.by_key]
                if not matches:
                    matches = [matched for key, body in requests if (matched := self._png_match(key, body)) is not None]
                if not matches and self.lookup is not None:
                    for key, _ in requests:
                        if key.id in self.looked_up:
                            continue
                        captures = self.lookup(key)
                        if any(
                            item.call.request != key or exchange != item.call.exchange_id
                            for exchange, item in captures.items()
                        ):
                            raise EvidenceUnavailable("Request lookup returned another request or capture")
                        self.inventory.captures.update(captures)
                        if captures:
                            self.image_indexes.pop((key.request_id, key.session_id), None)
                        self.looked_up.add(key.id)
                    self.by_key = replayable_captures(self.inventory)
                    matches = [(key, body) for key, body in requests if key.id in self.by_key]
                if not matches:
                    matches = [
                        matched
                        for key, body in requests
                        if (matched := self._png_match(key, body, allow_tolerance=True)) is not None
                    ]
                if len(matches) != 1:
                    if len(matches) > 1:
                        raise ReplayMismatch("More than one saved provider matches this model request")
                    saved_providers = {item.call.request.provider for item in self.inventory.captures.values()}
                    key, body = next((pair for pair in requests if pair[0].provider in saved_providers), requests[0])
                    self._mode = key.provider
                    return key, body
                key, body = matches[0]
                self._mode = key.provider
                self._provider_selected = True
                actual = next(candidate for candidate, _ in requests if candidate.provider == key.provider)
                self.emit("provider_selected", provider=key.provider, call_id=actual.id)
                return key, body
            except Exception as error:
                for key, body in requests:
                    self.emit("model_request", call_id=key.id, request_key=key.model_dump(), body=body.decode())
                self.problem = error
                self.emit("request_mismatch", error=str(error))
                raise

    def _recorded_response(self, key: RequestKey, body: bytes) -> CapturedResponse:
        actual = RequestKey.from_body(key.provider, body, key.request_id, session_id=key.session_id)
        if key.id in self.by_key:
            self.request_matches[actual.id] = (
                "exact"
                if actual.id == key.id
                else "image_tolerance" if actual.id in self.image_tolerance_matches else "exact_png_pixels"
            )
        self.emit(
            "model_request",
            call_id=actual.id,
            request_key=actual.model_dump(),
            recorded_request_key=key.model_dump(),
            body=body.decode(),
        )
        if key.id not in self.by_key:
            attempts = [c for c in self.inventory.captures.values() if c.call.request == key]
            if not any(c.call.authorized or c.call.manifest_sha256 is not None for c in attempts):
                self.assumed_failures.add(key.id)
                self.unknown_usage.add(key.id)
                self.emit(
                    "assumed_connection_failure",
                    call_id=actual.id,
                    request_key=key.model_dump(),
                    reason="No recorded capture",
                )
                raise WitnessError("No recorded capture for this request", code="capture_unrecorded", source="gateway")
        try:
            item = self.by_key.get(key.id)
            if item is None:
                attempts = [c for c in self.inventory.captures.values() if c.call.request == key]
                if attempts:
                    raise EvidenceUnavailable("Matching call has no usable completed capture")
                raise ReplayMismatch("No saved request matches this model request")
            if item.call.request != key:
                raise ReplayMismatch("Captured request identity differs")
            item, response = self.fetch(item.call.exchange_id)
            if item.call.request != key or item.call.phase not in ("issued", "failed"):
                raise EvidenceUnavailable("Matched capture has no recorded outcome")
            captured = None
            if item.call.manifest_sha256 is not None:
                if hashlib.sha256(response).hexdigest() != item.response_sha256 or item.provider_status is None:
                    raise EvidenceUnavailable("Saved response changed after loading")
                captured = CapturedResponse(item.call.exchange_id, item.provider_status, response)
            elif item.call.phase != "failed" or not item.call.authorized or item.call.failure is None or response:
                raise EvidenceUnavailable("Matched capture has no recorded failure")
            if item.usage is None:
                self.unknown_usage.add(key.id)
                self.emit(
                    "usage_excluded",
                    call_id=actual.id,
                    exchange_id=item.call.exchange_id,
                    reason="No recorded token usage",
                )
            else:
                self.budget.consume(key.id, item.model, item.usage, not_dispatched=item.call.not_dispatched)
            self.used.add(item.call.exchange_id)
            if captured is not None:
                self.emit(
                    "model_response",
                    call_id=actual.id,
                    exchange_id=item.call.exchange_id,
                    provider_status=item.provider_status,
                    usage=item.usage,
                    body=response.decode(),
                )
        except Exception as error:
            self.problem = error
            self.emit("request_mismatch", call_id=actual.id, error=str(error))
            raise
        if captured is None or (item.call.phase == "failed" and captured.status == 200):
            message = "Recorded witness capture failed" if captured is None else "Unusable provider response"
            self.emit(
                "recorded_capture_error",
                call_id=actual.id,
                exchange_id=item.call.exchange_id,
                failure=item.call.failure,
                code="capture_failed",
                source="gateway",
                error=message,
            )
            raise WitnessError(message, item.call.exchange_id, code="capture_failed", source="gateway")
        return captured

    def finish(self) -> None:
        """Raise a request mismatch even if the miner caught it while running."""
        if self.problem is not None:
            raise self.problem


class _AsyncRecordedStream(AsyncModelStream):
    """Keep the miner's logged stream reads and closes off its event loop."""

    async def __anext__(self) -> ResponseStreamEvent:
        try:
            event = await asyncio.to_thread(next, self._stream, None)
        except BaseException:
            await self.aclose()
            raise
        if event is None:
            raise StopAsyncIteration
        return event

    async def aclose(self) -> None:
        await asyncio.to_thread(self._stream.close)


class AsyncRecordedWitnessClient(AsyncWitnessClient):
    """Let async miners use the same recorded answers and fresh renderer."""

    def __init__(self, witness: RecordedWitnessClient) -> None:
        self._recorded = witness
        self.responses = self

    @overload
    async def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[False] = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response: ...

    @overload
    async def create(
        self,
        *,
        request_id: int = 0,
        stream: Literal[True],
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> AsyncModelStream: ...

    async def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | AsyncModelStream:
        if stream:
            result = await asyncio.to_thread(
                lambda: self._recorded.create(request_id=request_id, stream=True, session_id=session_id, **params)
            )
            return _AsyncRecordedStream(result)
        return await asyncio.to_thread(
            lambda: self._recorded.create(request_id=request_id, stream=False, session_id=session_id, **params)
        )

    async def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        return await asyncio.to_thread(self._recorded.render, source, options)

    async def resume(self, exchange_id: str) -> Response:
        return self._recorded.resume(exchange_id)

    async def get_submitted(self) -> dict[str, dict[str, object]]:
        return self._recorded.get_submitted()

    async def submit(self, image: bytes, source: str | None) -> dict[str, object]:
        return self._recorded.submit(image, source)

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self
