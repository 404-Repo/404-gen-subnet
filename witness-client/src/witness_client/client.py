"""Send OpenAI requests through the subnet witness."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import ssl
import threading
import time
import uuid
import zlib
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self, Unpack, cast, overload
from urllib.parse import urlsplit

import httpx
from bittensor_wallet import Keypair
from openai.types.responses import Response
from openai.types.responses.response_create_params import (
    ResponseCreateParamsBase,
    ResponseCreateParamsStreaming,
)
from renderer_client import RenderOptions, RenderUnavailable
from renderer_client.client import DEFAULT_OPTIONS, _render_response, _source_bytes
from websockets.exceptions import WebSocketException

from . import request as openai_request
from .calls import RequestKey
from .delivery import _raise_response_error
from .errors import ExchangePending, WitnessError
from .pods import PodGatewayClient
from .proof import Prover
from .response import ModelStream, model_error, model_result
from .timing import identify_exchange, measure, timed
from .transport import exchange
from .tunnel import WebSocketTunnel
from .types import (
    Claim,
    EarlyDisclosure,
    HandshakeClaim,
    HandshakeDisclosure,
    Session,
    WitnessConfig,
)

if TYPE_CHECKING:
    from ._tls import TLSAbruptCloseError
else:
    from tlslite.errors import TLSAbruptCloseError


@dataclass(frozen=True)
class CaptureContext:
    """Local capture namespace, storage and delivery settings."""

    namespace: str
    provider: Literal["api", "codex"]
    state_directory: Path
    delivery_timeout: float


@dataclass(frozen=True)
class CapturedResponse:
    exchange_id: str
    status: int
    body: bytes


class _ClientState:
    """Configuration and canonical requests shared by both clients."""

    def __init__(
        self,
        config: WitnessConfig | str | os.PathLike[str],
        *,
        hotkey: Keypair,
        openai_api_key: str | None,
        subscription: Literal["codex"] | None,
        delivery_timeout: float,
        parallel_calls: int,
        parallel_preparations: int,
    ) -> None:
        self._config: WitnessConfig
        if isinstance(config, dict):
            self._config = config.copy()
        else:
            self._config = json.loads(Path(config).read_text(encoding="utf-8"))
        if (
            not isinstance(parallel_calls, int)
            or isinstance(parallel_calls, bool)
            or not 1 <= parallel_calls <= 32
        ):
            raise ValueError("parallel_calls must be an integer from 1 through 32")
        if (
            not isinstance(parallel_preparations, int)
            or isinstance(parallel_preparations, bool)
            or not 1 <= parallel_preparations <= 32
        ):
            raise ValueError(
                "parallel_preparations must be an integer from 1 through 32"
            )
        if not math.isfinite(delivery_timeout) or delivery_timeout <= 0:
            raise ValueError("delivery_timeout must be finite and > 0 seconds")
        if (openai_api_key is None) == (subscription is None) or subscription not in (
            None,
            "codex",
        ):
            raise ValueError("Supply either openai_api_key or subscription='codex'")
        self._mode: Literal["api", "codex"] = (
            "codex" if subscription is not None else "api"
        )
        self._api_key = openai_api_key
        base = self._config["witness_url"].rstrip("/")
        url = urlsplit(base)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("Witness URL must be an HTTPS origin")
        state_directory = self._config.get("state_directory")
        if state_directory is not None:
            self._state = Path(state_directory).expanduser()
        else:
            state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
            if not state_home.is_absolute():
                state_home = Path.home() / ".local" / "state"
            self._state = state_home / "404-witness-client"
        self._parameters = Path(self._config["proof_parameters"]).expanduser().resolve()

        self._hotkey = hotkey

    def _request(
        self, request_id: int, session_id: str | None, params: ResponseCreateParamsBase
    ) -> tuple[RequestKey, bytes]:
        openai_request.validate_session_id(session_id, self._mode)
        normalized = cast(
            ResponseCreateParamsStreaming,
            {"service_tier": "default", **params, "stream": True},
        )
        body = openai_request.serialize_body(normalized, self._mode)
        return RequestKey.from_body(
            self._mode, body, request_id, session_id=session_id
        ), body


class WitnessClient(_ClientState):
    """Responses calls with durable allocation-scoped capture and recovery."""

    @timed("initialize")
    def __init__(
        self,
        config: WitnessConfig | str | os.PathLike[str],
        *,
        hotkey: Keypair,
        openai_api_key: str | None = None,
        subscription: Literal["codex"] | None = None,
        delivery_timeout: float = 600,
        parallel_calls: int = 3,
        parallel_preparations: int = 3,
        call_slots: threading.Semaphore | None = None,
    ) -> None:
        """Configure credentials, proof parameters, and capture concurrency.

        Args:
            config: WitnessConfig mapping or path to its JSON file. Requires
                witness_url (HTTPS origin) and proof_parameters path.
                state_directory is a path hint for optional execution hosts;
                the SDK does not create it or use it for recovery.
            hotkey: Miner signing key loaded and managed by the caller.
            openai_api_key: OpenAI API key. Choose this or subscription.
            subscription: Use "codex" for an existing local Codex login.
                Credentials are refreshed when needed.
            delivery_timeout: Positive wait/retry window in seconds for each
                delivery phase (default 600), including capture and recovery.
                It is not a total create() time limit.
            parallel_calls: Maximum active captures across all calls (1 through
                32, default 3). Completed response recovery bypasses this limit.
            parallel_preparations: Maximum concurrent Pod call preparations
                across calls (1 through 32, default 3).
        """
        super().__init__(
            config, hotkey=hotkey, openai_api_key=openai_api_key,
            subscription=subscription, delivery_timeout=delivery_timeout,
            parallel_calls=parallel_calls, parallel_preparations=parallel_preparations,
        )
        with measure("prover_initialize"):
            self._prover = Prover(self._parameters)
        self._capture_slots = threading.BoundedSemaphore(parallel_calls)
        self._preparation = threading.BoundedSemaphore(parallel_preparations)
        self._call_slots = call_slots
        self._calls_lock = threading.Lock()
        self._calls: dict[str, threading.Lock] = {}
        self._hotkey = hotkey
        with measure("http_initialize"):
            context = ssl.create_default_context()
            self._http = httpx.Client(
                base_url=self._config["witness_url"].rstrip("/"),
                verify=context,
                timeout=30,
                follow_redirects=False,
                trust_env=False,
            )
        self._gateway = PodGatewayClient(
            self._http,
            self._hotkey,
            self._state,
            delivery_timeout,
        )
        # Preserve the familiar Responses API spelling without an extra layer.
        self.responses = self

    @property
    def capture_context(self) -> CaptureContext:
        """Metadata for trusted callers using capture and recovery."""
        self._gateway.ensure_pod()
        assert self._gateway.round_number is not None
        namespace = hashlib.sha256(
            f"{self._http.base_url}/{self._hotkey.ss58_address}/"
            f"{self._gateway.round_number}/{self._mode}".encode()
        ).hexdigest()
        return CaptureContext(
            namespace=namespace,
            provider=self._mode,
            state_directory=self._state,
            delivery_timeout=self._gateway.delivery_timeout,
        )

    def capture_response(self, key: RequestKey, body: bytes) -> CapturedResponse:
        """Capture or recover the logical call identified by its canonical request."""
        return self._resolve(key, body, key.session_id)

    def saved_response(self, exchange_id: str) -> CapturedResponse:
        """Retrieve and verify retained Pod evidence without issuing inference."""
        return self._recover(exchange_id)

    def close(self) -> None:
        """Close the client's HTTP connections."""
        self._gateway.close()
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

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

    @timed("create")
    def create(
        self,
        *,
        request_id: int = 0,
        stream: bool = False,
        session_id: str | None = None,
        **params: Unpack[ResponseCreateParamsBase],
    ) -> Response | ModelStream:
        """Repeat the same request and request_id to reuse its saved result.

        Change request_id for another model call with the same request.
        """
        key, body = self._request(request_id, session_id, params)
        try:
            captured = self._resolve(key, body, session_id)
        except WitnessError as error:
            raise model_error(error, self._mode) from error
        return model_result(captured.status, captured.body, self._mode, stream=stream)

    def renderer_transport(self) -> httpx.BaseTransport:
        """Authenticate RendererClient requests to this miner's assigned Pod."""
        return httpx.MockTransport(self._gateway.send)

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        """Render a preview through the owned Pod connection without retrying."""
        _source_bytes(source)
        request = self._http.build_request(
            "POST",
            "/v1/render/grid",
            params=options.query(),
            json={"source": source},
            timeout=240,
        )
        try:
            response = self._gateway.send(request)
            try:
                return _render_response(response)
            finally:
                response.close()
        except httpx.HTTPError as error:
            raise RenderUnavailable(f"Renderer request failed: {error}") from error

    def submit(self, image: bytes, source: str | None) -> dict[str, object]:
        """Submit the runner's returned result to the assigned Pod without retrying."""
        body = _submission_body(image, source)
        request = self._http.build_request("POST", "/v1/submissions", json=body, timeout=150)
        try:
            response = self._gateway.send(request)
        except httpx.HTTPError as error:
            raise WitnessError(
                "Final submission transport failed; check submitted results before another pass",
                code="submission_unresolved", source="gateway",
            ) from error
        try:
            return _submission_result(response)
        finally:
            response.close()

    def get_submitted(self) -> dict[str, dict[str, object]]:
        """Read this miner's saved round results, keyed by official prompt ID."""
        request = self._http.build_request("GET", "/v1/submissions")
        try:
            response = self._gateway.send(request)
        except httpx.HTTPError as error:
            raise WitnessError(
                "Could not retrieve submitted results",
                code="submission_lookup_failed", source="gateway",
            ) from error
        try:
            return _submissions_result(response)
        finally:
            response.close()

    @timed("resume")
    def resume(self, exchange_id: str) -> Response:
        """Retrieve a verified Pod recording using the same hotkey and provider."""
        captured = self._recover(exchange_id)
        return cast(Response, model_result(captured.status, captured.body, self._mode))

    def _recover(self, exchange_id: str) -> CapturedResponse:
        identifier = str(uuid.UUID(exchange_id))
        identify_exchange(identifier)
        deadline = time.monotonic() + self._gateway.delivery_timeout
        while True:
            snapshot = self._gateway.snapshot(identifier, deadline=deadline)
            if (
                snapshot.call.exchange_id != identifier
                or snapshot.hotkey != self._hotkey.ss58_address
                or snapshot.call.request.provider != self._mode
            ):
                raise WitnessError("Witness recording differs", identifier, code="invalid_recording")
            if snapshot.call.manifest_sha256 is not None:
                _session, status, response = self._gateway.recorded(
                    snapshot.call.request, snapshot.call, self._prover.verifying_key_sha256,
                )
                if snapshot.call.phase == "failed" and status == 200:
                    raise WitnessError("Unusable provider response", identifier, code="capture_failed", source="gateway")
                return CapturedResponse(identifier, status, response)
            if snapshot.call.phase == "failed":
                raise WitnessError(
                    "Recorded witness capture failed", identifier, code="capture_failed", source="gateway",
                )
            if time.monotonic() >= deadline:
                raise ExchangePending("Capture remains unresolved", identifier)
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def _handshake(
        self, tunnel: WebSocketTunnel, disclosure: HandshakeDisclosure, *, deadline: float,
    ) -> None:
        """Verify TLS and its fixed HEAD probe before slow proof preparation."""
        raw = _encode_json(_disclosure_claim(disclosure))
        with measure("handshake_verification_wait"):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Capture wait timed out before handshake verification")
            tunnel.verify(
                "handshake",
                raw,
                timeout,
                deadline=deadline,
            )

    def _authorize(
        self, tunnel: WebSocketTunnel, disclosure: EarlyDisclosure, *, deadline: float,
    ) -> None:
        """Commit disclosure before the witness releases this one provider request."""
        if time.monotonic() >= deadline:
            raise TimeoutError("Capture wait timed out before credential proof")
        with measure("credential_proof"):
            credential = self._prover.prove(disclosure["private"])
        claim: Claim = {
            "credential": credential,
            **_disclosure_claim(disclosure),
        }
        with measure("claim_encode"):
            raw = _encode_json(claim)
        with measure("authorization_verification_wait"):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Capture wait timed out before authorization")
            tunnel.verify(
                "authorize",
                raw,
                timeout,
                deadline=deadline,
            )

    def _resolve(
        self, key: RequestKey, body: bytes, session_id: str | None
    ) -> CapturedResponse:
        with self._calls_lock:
            lock = self._calls.setdefault(key.id, threading.Lock())
        with lock:
            return self._capture(key, body, session_id)

    def _capture(
        self, key: RequestKey, body: bytes, session_id: str | None
    ) -> CapturedResponse:
        identifier: str | None = None
        retries = 0
        raw_response: bytes | None = None
        try:
            snapshot = self._gateway.start(key)
            if snapshot.call.phase == "failed" and (
                snapshot.call.authorized or not snapshot.call.not_dispatched
            ):
                snapshot = self._gateway.start(key, expected_exchange_id=snapshot.call.exchange_id)
            identifier = snapshot.call.exchange_id
            identify_exchange(identifier)
            if (
                snapshot.call.request != key
                or snapshot.hotkey != self._hotkey.ss58_address
            ):
                raise WitnessError(
                    "Witness call differs", identifier, code="invalid_recording"
                )
            attempted = False
            needs_capture = snapshot.call.phase not in ("issued", "failed") or (
                snapshot.call.phase == "failed" and snapshot.call.not_dispatched and not snapshot.call.authorized
            )
            with (
                self._capture_slots if needs_capture else nullcontext(),
                self._call_slots
                if self._call_slots is not None and needs_capture
                else nullcontext(),
                ExitStack() as preparation,
            ):
                # Bound live preparation before opening provider connections.
                # Release its slot after authorization so inference can overlap.
                if snapshot.call.phase in ("reserved", "assigned"):
                    preparation.enter_context(self._preparation)
                delivery_deadline = time.monotonic() + self._gateway.delivery_timeout
                deadline = delivery_deadline
                if snapshot.call.authorized and snapshot.call.deadline is not None:
                    deadline = min(deadline, time.monotonic() + snapshot.call.deadline - time.time())
                while snapshot.call.phase != "issued":
                    if snapshot.call.phase == "failed" and (
                        snapshot.call.authorized or not snapshot.call.not_dispatched
                    ):
                        break
                    if (
                        snapshot.call.phase == "failed"
                        and snapshot.call.not_dispatched
                        and not snapshot.call.authorized
                    ) or (snapshot.call.phase == "assigned" and attempted):
                        expected_exchange_id = None
                        if snapshot.call.phase == "failed":
                            if retries >= 2:
                                break
                            retries += 1
                            deadline = delivery_deadline
                            expected_exchange_id = snapshot.call.exchange_id
                            time.sleep(min(retries, max(0, deadline - time.monotonic())))
                        if time.monotonic() >= deadline:
                            raise WitnessError(
                                "Capture wait timed out", identifier, code="capture_timeout", source="gateway"
                            )
                        snapshot = self._gateway.start(
                            key, expected_exchange_id=expected_exchange_id, deadline=deadline
                        )
                        if (
                            snapshot.call.request != key
                            or snapshot.hotkey != self._hotkey.ss58_address
                        ):
                            raise WitnessError(
                                "Witness retry differs", code="invalid_recording"
                            )
                        identifier = snapshot.call.exchange_id
                        identify_exchange(identifier)
                        deadline = delivery_deadline
                        if snapshot.call.authorized and snapshot.call.deadline is not None:
                            deadline = min(deadline, time.monotonic() + snapshot.call.deadline - time.time())
                        attempted = False
                        raw_response = None
                        preparation.close()
                        if snapshot.call.phase in ("reserved", "assigned"):
                            preparation.enter_context(self._preparation)
                        continue
                    if not attempted and snapshot.call.phase != "failed":
                        if snapshot.session is not None:
                            session = cast(Session, snapshot.session)
                            host, path = openai_request.PROVIDERS[self._mode]
                            if any(
                                session.get(k) != v
                                for k, v in {
                                    "id": identifier,
                                    "protocol": 2,
                                    "provider": f"https://{host}{path}",
                                    "proof_key_sha256": self._prover.verifying_key_sha256,
                                    "miner": self._hotkey.ss58_address,
                                }.items()
                            ):
                                raise WitnessError(
                                    "Witness session differs",
                                    identifier,
                                    code="invalid_recording",
                                )
                            attempted = True
                            request = openai_request.prepare_request(
                                self._config,
                                self._mode,
                                self._api_key,
                                body=body,
                                session_id=session_id,
                            )
                            try:
                                with self._gateway.tunnel(identifier, deadline=deadline) as tunnel:

                                    def authorize(disclosure: EarlyDisclosure) -> None:
                                        self._authorize(tunnel, disclosure, deadline=deadline)
                                        preparation.close()

                                    raw_response = exchange(
                                        tunnel,
                                        request.host,
                                        request.chunks,
                                        handshake=lambda disclosure: self._handshake(
                                            tunnel, disclosure, deadline=deadline
                                        ),
                                        authorize=authorize,
                                        timeout=max(0, deadline - time.monotonic()),
                                    )
                            except (
                                OSError,
                                TimeoutError,
                                WebSocketException,
                                WitnessError,
                                TLSAbruptCloseError,
                            ):
                                # Recover a claimed exchange that may already be paying/capturing.
                                snapshot = self._gateway.snapshot(identifier, deadline=deadline)
                    if snapshot.call.phase == "failed":
                        continue
                    if time.monotonic() >= deadline:
                        raise WitnessError(
                            "Capture wait timed out",
                            identifier,
                            code="capture_timeout",
                            source="gateway",
                        )
                    snapshot = self._gateway.snapshot(identifier, deadline=deadline)
                    if snapshot.call.phase != "issued":
                        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            if snapshot.call.phase == "failed" and snapshot.call.manifest_sha256 is None:
                raise WitnessError(
                    "Recorded witness capture failed",
                    identifier,
                    code="capture_failed",
                    source="gateway",
                )
            session, status, response = self._gateway.recorded(
                key,
                snapshot.call,
                self._prover.verifying_key_sha256,
                request=body,
                raw_response=raw_response,
            )
            if snapshot.call.phase == "failed" and status == 200:
                raise WitnessError("Unusable provider response", identifier, code="capture_failed", source="gateway")
            return CapturedResponse(identifier, status, response)
        except WitnessError as error:
            error.exchange_id = error.exchange_id or identifier
            raise
        except httpx.TimeoutException as error:
            raise WitnessError(
                "Witness connection timed out", identifier, code="capture_timeout", source="gateway"
            ) from error
        except Exception as error:
            raise WitnessError(
                "Witness capture did not complete",
                identifier,
                code="capture_unresolved",
                source="gateway",
            ) from error


def _encode_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _disclosure_claim(disclosure: HandshakeDisclosure) -> HandshakeClaim:
    compression = zlib.compressobj(wbits=-15)
    encoded = (
        compression.compress(_encode_json(disclosure["disclosures"]))
        + compression.flush()
    )
    return {
        "disclosures": base64.b64encode(encoded).decode(),
        "response_key": disclosure["response_key"],
        "client_bytes": disclosure["client_bytes"],
        "probe": True,
    }


def _submission_body(image: bytes, source: str | None) -> dict[str, object]:
    if not isinstance(image, bytes):
        raise ValueError("image must contain the frozen prompt bytes")
    if len(image) > 8 * 1024 * 1024:
        raise ValueError("Prompt image exceeds 8 MiB")
    if source is not None:
        if not isinstance(source, str):
            raise ValueError("generate() must return JavaScript or None")
        if len(source.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Source exceeds the renderer's 1 MiB limit")
    return {
        "generation": {"prompt_sha256": hashlib.sha256(image).hexdigest()},
        "source": source,
        "prompt_image": base64.b64encode(image).decode("ascii") if source is not None else None,
    }


def _submission_result(response: httpx.Response) -> dict[str, object]:
    if len(response.content) > 256 * 1024:
        raise WitnessError("Oversized final submission response", code="invalid_submission", source="gateway")
    if not response.is_success:
        _raise_response_error(response, response.content)
    try:
        value = response.json()["submission"]
        if not isinstance(value, dict):
            raise ValueError("Expected a saved submission")
        return cast(dict[str, object], value)
    except (ValueError, KeyError, TypeError) as error:
        raise WitnessError("Invalid final submission response", code="invalid_submission", source="gateway") from error


def _submissions_result(response: httpx.Response) -> dict[str, dict[str, object]]:
    # The saved index is bounded to 16 MiB; the JSON envelope adds 16 bytes.
    if len(response.content) > 16 * 1024 * 1024 + 16:
        raise WitnessError("Oversized submissions response", code="invalid_submissions", source="gateway")
    if not response.is_success:
        _raise_response_error(response, response.content)
    try:
        value = response.json()["submissions"]
        if not isinstance(value, dict) or any(
            not isinstance(prompt, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", prompt) is None
            or not isinstance(result, dict)
            for prompt, result in value.items()
        ):
            raise ValueError("Expected saved submissions keyed by official prompt ID")
        return cast(dict[str, dict[str, object]], value)
    except (ValueError, KeyError, TypeError) as error:
        raise WitnessError("Invalid submissions response", code="invalid_submissions", source="gateway") from error
