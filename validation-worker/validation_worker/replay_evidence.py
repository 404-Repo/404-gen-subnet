"""Read-only preparation of a miner's source, frozen inputs and saved R2 traffic."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from botocore.exceptions import ReadTimeoutError
from renderer_client import RenderOptions, RenderRejected, RenderRequest
from renderer_client.client import DEFAULT_OPTIONS, MAX_ERROR_BYTES, MAX_RESPONSE_BYTES, PNG_SIGNATURE
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.generations import GenerationsAdapter, get_generations
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.github import GitHubClient
from subnet_common.witness.models import Scope, json_bytes
from subnet_common.witness.rounds import RoundInputs
from subnet_common.witness.state import R2StateStore
from witness_client.calls import RequestKey, request_digest
from witness_client.pods import CallView
from witness_client.state import save
from witness_pod.records import Call, CallRecord

from .render_recovery import MissingRenderOutcome
from .request_replay import (
    CachedImageRequest,
    EvidenceUnavailable,
    ImageRequestIndex,
    PreparedReplay,
    RequestInventory,
    SavedCapture,
    png_request,
    replayable_captures,
)


R2_READ_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)


class Evidence(Protocol):
    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None: ...
    def keys(self, prefix: str) -> Iterable[str]: ...


class R2Evidence:
    def __init__(
        self,
        state: R2StateStore,
        keys: Callable[[str], Iterable[str]],
        *,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self.state, self._keys = state, keys
        self.emit = emit

    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        attempt = 1
        while True:
            try:
                return self.state.read(key, limit)
            except ReadTimeoutError as error:
                if attempt > len(R2_READ_RETRY_DELAYS):
                    raise
                delay = R2_READ_RETRY_DELAYS[attempt - 1]
                if self.emit is not None:
                    self.emit("r2_read_retry", key=key, attempt=attempt, delay=delay, error=str(error))
                time.sleep(delay)
                attempt += 1

    def keys(self, prefix: str) -> Iterable[str]:
        return self._keys(prefix)


class SavedRenderer:
    """Read saved v2 preview outcomes and final grids."""

    def __init__(
        self,
        objects: Evidence,
        artifacts: Evidence,
        scope: Scope,
        *,
        cache: Path,
        artifact_cache: Path,
        storage_prefix: str = "",
        emit: Callable[..., None] | None = None,
    ) -> None:
        self.objects, self.artifacts = objects, artifacts
        self.renderer_id = "saved-renders"
        self._saved_keys: set[str] | None = None
        self._index_lock = threading.Lock()
        self.prefix = f"{storage_prefix}rounds/{scope.round}/{scope.hotkey}/"
        self.cache, self.artifact_cache, self.emit = cache, artifact_cache, emit

    @staticmethod
    def _path(cache: Path, key: str) -> Path:
        suffix = ".png" if key.endswith(".png") else ".rejection.json"
        return cache / (hashlib.sha256(key.encode()).hexdigest() + suffix)

    def _read(self, objects: Evidence, key: str, cache: Path) -> bytes | None:
        path = self._path(cache, key)
        reused = path.is_file()
        limit = MAX_RESPONSE_BYTES if key.endswith(".png") else MAX_ERROR_BYTES
        if reused and path.stat().st_size > limit:
            raise EvidenceUnavailable("Saved render exceeds the response size limit: " + key)
        data = path.read_bytes() if reused else objects.read(key, limit)
        if data is None:
            return None
        try:
            if len(data) > limit:
                raise ValueError("Render exceeds the response size limit")
            if key.endswith(".png"):
                if not data.startswith(PNG_SIGNATURE):
                    raise ValueError("Expected saved PNG bytes")
            else:
                RenderRejected(data.decode("utf-8"))
        except ValueError as error:
            raise EvidenceUnavailable("Invalid saved render outcome: " + key) from error
        if not reused:
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            save(path, data)
        if self.emit is not None:
            self.emit(
                "saved_render_loaded",
                key=key,
                cached=reused,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        return data

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        RenderRequest.from_source(self.renderer_id, source, options)
        request = httpx.Request(
            "POST", "https://witness.internal/v1/render/grid", params=options.query(), json={"source": source}
        )
        path, body_hash = request.url.raw_path.decode("ascii"), hashlib.sha256(request.content).hexdigest()
        request_hash = hashlib.sha256(json_bytes((path, body_hash))).hexdigest()
        key = self.prefix + "renders/v2/" + request_hash
        names = (key + ".png", key + ".rejection.json")
        for name in names:
            if self._path(self.cache, name).is_file():
                data = self._read(self.objects, name, self.cache)
                if data is not None:
                    if name.endswith(".png"):
                        return data
                    raise RenderRejected(data.decode("utf-8"))
        with self._index_lock:
            if self._saved_keys is None:
                self._saved_keys = set(self.objects.keys(self.prefix + "renders/v2/"))
            available = self._saved_keys
        for name in names:
            if name in available:
                data = self._read(self.objects, name, self.cache)
                if data is not None:
                    if name.endswith(".png"):
                        return data
                    raise RenderRejected(data.decode("utf-8"))
        raise MissingRenderOutcome(key)

    def final_render(self, prompt: str, source: str, expected_source: str | None, views: str | None) -> bytes:
        if source != expected_source:
            raise EvidenceUnavailable("Final source differs from the saved submission")
        root = self.prefix + "submitted/" + prompt
        if views != root:
            raise EvidenceUnavailable("No saved final grid for this submission: " + prompt)
        key = root + "/grid.png"
        data = self._read(self.artifacts, key, self.artifact_cache)
        if data is None:
            raise EvidenceUnavailable("Saved final grid is missing: " + key)
        return data


def required(objects: Evidence, key: str, limit: int = 16 * 1024 * 1024) -> bytes:
    data = objects.read(key, limit)
    if data is None:
        raise FileNotFoundError("Required saved evidence is missing: " + key)
    return data


def unpack_source(archive: bytes, destination: Path) -> None:
    """Materialize regular committed files without running installation hooks."""
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        roots = {p.filename.split("/", 1)[0] for p in source.infolist()}
        if len(roots) != 1:
            raise ValueError("Expected one repository archive root")
        for member in source.infolist():
            if member.is_dir():
                continue
            _, sep, name = member.filename.partition("/")
            path = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (
                not sep
                or not name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or path.as_posix() != name
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
            ):
                raise ValueError("Source archive cannot be safely staged: " + member.filename)
            target = destination / path
            if target.exists():
                raise ValueError("Duplicate source archive file")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(member))
            target.chmod(0o755 if mode & 0o111 else 0o644)
    if not (destination / "main.py").is_file():
        raise ValueError("Submitted commit has no root main.py")


def load_capture(
    objects: Evidence,
    scope: Scope,
    call: Call,
    directory: Path,
    pricing_hash: str,
    storage_prefix: str,
    *,
    cache: Path | None = None,
    previous: Path | None = None,
    emit: Callable[..., None] | None = None,
) -> SavedCapture:
    item = SavedCapture(call=CallView.model_validate(call.model_dump(exclude={"capture_id"})))
    directory.mkdir(mode=0o700, exist_ok=True)
    prefix = f"{storage_prefix}rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
    if call.manifest_sha256 is None and (call.phase != "failed" or not call.authorized or call.failure is None):
        raise ValueError("Capture has no recorded outcome: " + call.exchange_id)
    names = (
        ("manifest.json", "request.json", "response.sse")
        if call.manifest_sha256 is not None
        else ("request.json", "failure.json")
    )
    identity = hashlib.sha256(json_bytes([scope.key, storage_prefix, pricing_hash, item.call.model_dump()])).hexdigest()
    cached = None if cache is None else cache / identity
    reused = cached is not None and all((cached / name).is_file() for name in names)
    if (
        not reused
        and cached is not None
        and call.manifest_sha256 is None
        and all((cached / name).is_file() for name in ("request.json", "status.json"))
    ):
        names = ("request.json", "status.json")
        reused = True
    source = cached if reused else None
    if source is None and previous is not None:
        candidate = previous / call.exchange_id
        if all((candidate / name).is_file() for name in names) and (
            call.manifest_sha256 is None
            or hashlib.sha256((candidate / "manifest.json").read_bytes()).hexdigest() == call.manifest_sha256
        ):
            source = candidate
    prefetched = {}
    if source is None and call.manifest_sha256 is None:
        failure = objects.read(prefix + "failure.json", 32 * 1024 * 1024)
        if failure is not None:
            prefetched["failure.json"] = failure
        else:
            names = ("request.json", "status.json")
            if previous is not None:
                candidate = previous / call.exchange_id
                if all((candidate / name).is_file() for name in names):
                    source = candidate
    for name in names:
        if source is not None and (source / name).stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Cached evidence exceeds the saved response size limit")
        data = (
            (source / name).read_bytes()
            if source is not None
            else prefetched[name] if name in prefetched else required(objects, prefix + name, 32 * 1024 * 1024)
        )
        (directory / name).write_bytes(data)

    def remember() -> None:
        if cached is None:
            return
        if not reused:
            cached.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in names:
                save(cached / name, (directory / name).read_bytes())
        if emit is not None:
            emit(
                (
                    "saved_response_cache_hit"
                    if reused
                    else "saved_response_cache_imported" if source is not None else "saved_response_cached"
                ),
                exchange_id=call.exchange_id,
                path=str(cached),
            )

    if call.manifest_sha256 is None:
        request = (directory / "request.json").read_bytes()
        status_bytes = (directory / names[-1]).read_bytes()
        status = json.loads(status_bytes)
        if (
            request_digest(request) != call.request.request_sha256
            or status.get("status") != "failed"
            or status.get("error_type") != call.failure
        ):
            raise ValueError("Recorded failure binding differs: " + call.exchange_id)
        saved = SavedCapture(call=item.call, model=json.loads(request)["model"])
        remember()
        return saved
    manifest_bytes = (directory / "manifest.json").read_bytes()
    metadata = json.loads(manifest_bytes)
    request, response = (directory / "request.json").read_bytes(), (directory / "response.sse").read_bytes()
    binding = {
        "call_id": call.request.id,
        "request_key": call.request.model_dump(),
        "hotkey": scope.hotkey,
        "round": scope.round,
        "exchange_id": call.exchange_id,
    }
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != call.manifest_sha256
        or any(metadata.get(k) != value for k, value in binding.items())
        or request_digest(request) != call.request.request_sha256
        or metadata.get("request_sha256") != hashlib.sha256(request).hexdigest()
        or metadata.get("response_sha256") != hashlib.sha256(response).hexdigest()
    ):
        raise ValueError("Recorded evidence binding differs: " + call.exchange_id)
    saved = SavedCapture.model_validate(
        {
            "call": item.call.model_dump(),
            "model": json.loads(request)["model"],
            "usage": metadata.get("usage"),
            "response_sha256": metadata["response_sha256"],
            "provider_status": metadata["provider_status"],
        }
    )
    remember()
    return saved


def index_cache_path(index_cache: Path, scope: Scope, pricing_hash: str, storage_prefix: str) -> Path:
    identity = hashlib.sha256(json_bytes([scope.key, pricing_hash, storage_prefix])).hexdigest()
    return index_cache / (identity + ".json")


def lookup_request(
    objects: Evidence,
    scope: Scope,
    request: RequestKey,
    directory: Path,
    storage_prefix: str,
) -> dict[str, SavedCapture]:
    """Read the requested journal directly when it is absent from the cached index."""
    key = f"{storage_prefix}pod-calls/v2/{scope.deployment}/{scope.round}/{scope.hotkey}/{request.id}.json"
    raw = objects.read(key)
    if raw is None:
        return {}
    record = CallRecord.model_validate_json(raw)
    calls = (*record.attempts.values(), record.call)
    if record.scope_key != scope.key or any(call.request != request for call in calls):
        raise ValueError("Call journal belongs to another request or scope")
    journals = directory / "journals"
    journals.mkdir(exist_ok=True)
    (journals / (request.id + ".json")).write_bytes(raw)
    return {
        call.exchange_id: SavedCapture(call=CallView.model_validate(call.model_dump(exclude={"capture_id"})))
        for call in calls
    }


def load_image_request(
    objects: Evidence,
    scope: Scope,
    call: CallView,
    cache: Path,
    storage_prefix: str,
) -> bytes:
    """Load and cache a recorded request, checking its original digest on every read."""
    digest = call.request.request_sha256
    path = cache / (digest + ".request.json")
    reused = path.is_file()
    body = (
        path.read_bytes()
        if reused
        else required(
            objects,
            f"{storage_prefix}rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/request.json",
            8 * 1024 * 1024,
        )
    )
    if request_digest(body) != digest:
        raise ValueError("Recorded request digest differs: " + call.exchange_id)
    if not reused:
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        save(path, body)
    return body


def load_image_index(
    objects: Evidence,
    scope: Scope,
    inventory: RequestInventory,
    request_id: int,
    session_id: str | None,
    cache: Path,
    storage_prefix: str,
    emit: Callable[..., None],
) -> ImageRequestIndex:
    """Cache PNG-equivalent request digests after verifying the original request hash."""
    calls = {
        item.call.request.request_sha256: item.call
        for item in replayable_captures(inventory).values()
        if (item.call.request.request_id, item.call.request.session_id) == (request_id, session_id)
    }
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    emit("image_request_index_started", request_id=request_id, candidates=len(calls))

    def load(call: CallView) -> tuple[CachedImageRequest, bool]:
        digest = call.request.request_sha256
        path = cache / (digest + ".json")
        if path.is_file():
            saved = CachedImageRequest.model_validate_json(path.read_bytes())
            if saved.request_sha256 != digest:
                raise ValueError("Cached PNG request digest belongs to another request")
            return saved, True
        body = load_image_request(objects, scope, call, cache, storage_prefix)
        normalized = png_request(body)
        saved = CachedImageRequest(
            request_sha256=digest,
            image_request_sha256=hashlib.sha256(normalized).hexdigest() if normalized is not None else None,
        )
        save(path, saved.model_dump_json().encode())
        return saved, False

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(load, calls.values()))
    emit(
        "image_request_index_ready",
        request_id=request_id,
        requests=len(entries),
        reused=sum(reused for _, reused in entries),
        downloaded=sum(not reused for _, reused in entries),
    )
    return ImageRequestIndex(requests={item.request_sha256: item.image_request_sha256 for item, _ in entries})


class SavedReplaySource:
    """Load recorded requests and responses through evidence readers and local caches."""

    def __init__(
        self,
        objects: Evidence,
        replay_objects: Evidence,
        scope: Scope,
        directory: Path,
        inventory: RequestInventory,
        *,
        image_cache: Path,
        emit: Callable[..., None],
        response_cache: Path | None = None,
        previous: Path | None = None,
        storage_prefix: str = "",
    ) -> None:
        self.objects, self.replay_objects = objects, replay_objects
        self.scope, self.directory, self.inventory = scope, directory, inventory
        self.image_cache, self.response_cache = image_cache, response_cache
        self.previous, self.storage_prefix, self.emit = previous, storage_prefix, emit
        self.captures: dict[str, SavedCapture] = {}
        self.image_indexes: dict[tuple[int, str | None], dict[str, str | None]] = {}
        self.state_lock = threading.Lock()
        self.locks: dict[tuple[object, ...], threading.Lock] = {}

    def _resource_lock(self, *key: object) -> threading.Lock:
        with self.state_lock:
            return self.locks.setdefault(key, threading.Lock())

    def lookup(self, request: RequestKey) -> dict[str, SavedCapture]:
        with self._resource_lock("lookup", request.id):
            self.emit("request_lookup_started", call_id=request.id, request_key=request.model_dump())
            captures = lookup_request(self.objects, self.scope, request, self.directory, self.storage_prefix)
            with self._resource_lock("images", request.request_id, request.session_id):
                with self.state_lock:
                    self.inventory.captures.update(captures)
                    save(self.directory / "requests.json", (self.inventory.model_dump_json(indent=2) + "\n").encode())
                if captures:
                    self.image_indexes.pop((request.request_id, request.session_id), None)
            self.emit("request_lookup_finished", call_id=request.id, captures=len(captures))
            return captures

    def image_index(self, request_id: int, session_id: str | None) -> dict[str, str | None]:
        group = request_id, session_id
        with self._resource_lock("images", *group):
            if group not in self.image_indexes:
                with self.state_lock:
                    snapshot = self.inventory.model_copy(update={"captures": dict(self.inventory.captures)})
                self.image_indexes[group] = load_image_index(
                    self.replay_objects,
                    self.scope,
                    snapshot,
                    request_id,
                    session_id,
                    self.image_cache,
                    self.storage_prefix,
                    self.emit,
                ).requests
            return self.image_indexes[group]

    def request_body(self, request: RequestKey) -> bytes:
        with self._resource_lock("image_body", request.request_sha256):
            with self.state_lock:
                call = replayable_captures(self.inventory)[request.id].call
            return load_image_request(self.replay_objects, self.scope, call, self.image_cache, self.storage_prefix)

    def fetch(self, exchange_id: str) -> tuple[SavedCapture, bytes]:
        with self._resource_lock("capture", exchange_id):
            capture_dir = self.directory / exchange_id
            if exchange_id not in self.captures:
                self.emit("saved_response_load_started", exchange_id=exchange_id)
                with self.state_lock:
                    call = Call.model_validate(self.inventory.captures[exchange_id].call.model_dump())
                item = load_capture(
                    self.replay_objects,
                    self.scope,
                    call,
                    capture_dir,
                    self.inventory.pricing_sha256,
                    self.storage_prefix,
                    cache=self.response_cache,
                    previous=self.previous,
                    emit=self.emit,
                )
                (capture_dir / "capture.json").write_text(item.model_dump_json(indent=2) + "\n")
                self.captures[exchange_id] = item
                self.emit("saved_response_loaded", exchange_id=exchange_id, usage=item.usage, phase=item.call.phase)
            item = self.captures[exchange_id]
            body = (capture_dir / "response.sse").read_bytes() if item.response_sha256 is not None else b""
            return item, body


def export_requests(
    objects: Evidence,
    scope: Scope,
    output: Path,
    pricing: bytes,
    emit: Callable[..., None],
    *,
    storage_prefix: str = "",
    index_cache: Path | None = None,
    refresh_index: bool = False,
) -> RequestInventory:
    output.mkdir(mode=0o700)
    pricing_hash = hashlib.sha256(pricing).hexdigest()
    cached = None if index_cache is None else index_cache_path(index_cache, scope, pricing_hash, storage_prefix)
    if cached is not None and cached.is_file() and not refresh_index:
        snapshot = Path(json.loads(cached.read_bytes()))
        if (snapshot / "requests.json").is_file():
            inventory = RequestInventory.model_validate_json((snapshot / "requests.json").read_bytes())
            if inventory.scope_key != scope.key:
                raise ValueError("Cached request index scope differs; use --refresh-index")
            shutil.copyfile(snapshot / "requests.json", output / "requests.json")
            (output / "pricing.json").write_bytes(pricing)
            if (snapshot / "journals").is_dir():
                shutil.copytree(snapshot / "journals", output / "journals")
            emit("request_index_reused", captures=len(inventory.captures), source=str(snapshot / "requests.json"))
            return inventory
    inventory = RequestInventory(scope_key=scope.key, pricing_sha256=pricing_hash, captures={})
    prefix = f"{storage_prefix}pod-calls/v2/{scope.deployment}/{scope.round}/{scope.hotkey}/"
    for key in sorted(objects.keys(prefix)):
        record = CallRecord.model_validate_json(required(objects, key))
        if (
            record.scope_key != scope.key
            or key != prefix + record.call.request.id + ".json"
            or any(c.request != record.call.request for c in record.attempts.values())
        ):
            raise ValueError("Call journal belongs to another request or scope")
        journals = output / "journals"
        journals.mkdir(exist_ok=True)
        (journals / (record.call.request.id + ".json")).write_bytes(record.model_dump_json().encode())
        for call in (*record.attempts.values(), record.call):
            if call.exchange_id in inventory.captures:
                continue
            item = SavedCapture(call=CallView.model_validate(call.model_dump(exclude={"capture_id"})))
            inventory.captures[call.exchange_id] = item
            emit("capture_indexed", exchange_id=call.exchange_id, call_id=call.request.id, phase=call.phase)
    (output / "pricing.json").write_bytes(pricing)
    (output / "requests.json").write_text(inventory.model_dump_json(indent=2) + "\n")
    if cached is not None:
        cached.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        save(cached, json_bytes(str(output.resolve())))
        emit("request_index_saved", captures=len(inventory.captures), source=str(output / "requests.json"))
    return inventory


def resolve_hotkey(hotkey: str, submissions: dict[str, MinerSubmission], full_round: str) -> str:
    matches = sorted(
        key
        for key, submission in submissions.items()
        if hotkey and key.startswith(hotkey) and submission.round == full_round
    )
    if not matches:
        raise ValueError(f"No frozen source submission matches hotkey {hotkey!r} for round {full_round}")
    if len(matches) > 1:
        raise ValueError(f"Hotkey prefix {hotkey!r} is ambiguous: {', '.join(matches)}")
    return matches[0]


async def prepare(
    *,
    git: GitHubClient,
    objects: Evidence,
    round_name: str,
    hotkey: str,
    deployment: str,
    output: Path,
    pricing: bytes,
    emit: Callable[..., None],
    branch: str = "main",
    prompt_id: str | None = None,
    index_cache: Path | None = None,
    refresh_index: bool = False,
) -> PreparedReplay:
    """Resolve authority from Git and frozen R2 inputs; never write to either service."""
    ref = await git.get_ref_sha("heads/" + branch)
    config = await require_competition_config(git, ref)
    number = round_name.removeprefix(config.name + "-")
    if not re.fullmatch(r"0|[1-9][0-9]*", number):
        raise ValueError("Round must be a number or the competition's full round name")
    round_num = int(number)
    full_round = f"{config.name}-{round_num}"
    submissions = await require_submissions(git, round_num, ref)
    resolved_hotkey = resolve_hotkey(hotkey, submissions, full_round)
    scope = Scope(deployment, full_round, resolved_hotkey)
    if resolved_hotkey != hotkey:
        emit("hotkey_resolved", prefix=hotkey, hotkey=resolved_hotkey, round=full_round)
    hotkey = resolved_hotkey
    submission = submissions[hotkey]
    if (
        re.fullmatch(r"[\w.-]+/[\w.-]+", submission.repo) is None
        or re.fullmatch(r"[a-f0-9]{40}", submission.commit) is None
    ):
        raise ValueError("Expected a GitHub repository and full frozen commit")
    schedule = await require_schedule(git, round_num, ref)
    seed = await require_seed_from_git(git, round_num, ref)
    prompt_urls = await require_prompts(git, round_num, ref)
    identity = hashlib.sha256(json_bytes([git.repo.lower(), branch])).hexdigest()
    inputs = RoundInputs.model_validate_json(
        required(objects, f"pod-gateway/round-inputs/v1/{identity}/{full_round}.json")
    )
    if (
        inputs.repository != git.repo.lower()
        or inputs.branch != branch
        or inputs.round != full_round
        or inputs.number != round_num
        or inputs.seed != seed
        or inputs.storage_prefix != schedule.witness_storage_prefix
        or [p.id for p in inputs.prompts] != [PurePosixPath(urlsplit(url).path).stem for url in prompt_urls]
    ):
        raise ValueError("Retained round inputs differ from the official round")
    selected = [p for p in inputs.prompts if prompt_id is None or p.id == prompt_id]
    if not selected:
        raise ValueError("Prompt ID is not in the frozen round")
    emit("source_fetch", repository=submission.repo, commit=submission.commit, competition_ref=ref)
    commit = await git.client.get(f"/repos/{submission.repo}/git/commits/{submission.commit}")
    commit.raise_for_status()
    if commit.json()["sha"] != submission.commit:
        raise ValueError("GitHub source commit differs from the frozen submission")
    archive = await git.client.get(f"/repos/{submission.repo}/zipball/{submission.commit}", follow_redirects=True)
    archive.raise_for_status()
    unpack_source(archive.content, output / "source")
    root = f"{inputs.storage_prefix}rounds/{full_round}/{hotkey}/"
    saved = GenerationsAdapter.validate_json(required(objects, root + "submitted.json"))
    collected = await get_generations(git, round_num, hotkey, ref)
    prompts = []
    for prompt in selected:
        data = required(objects, prompt.image_key, 8 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != prompt.sha256:
            raise ValueError("Frozen prompt image hash differs: " + prompt.id)
        directory = output / "prompts" / prompt.id
        directory.mkdir(parents=True)
        (directory / "image.png").write_bytes(data)
        result = saved.get(prompt.id)
        expected: dict[str, object] = {"available": False, "reason": "not_submitted"}
        if result is not None:
            if prompt.id in collected and (
                collected[prompt.id].size != result.size
                or collected[prompt.id].failure_reason != result.failure_reason
                or ((collected[prompt.id].js is None) != (result.js is None))
                or (result.js is not None and not (collected[prompt.id].js or "").endswith("/" + result.js))
            ):
                raise ValueError("Collected and independently saved submission differ: " + prompt.id)
            if result.js is not None:
                if result.js != root + "submitted/" + prompt.id + ".js":
                    raise ValueError("Final source location differs from the saved round/hotkey/prompt")
                final = required(objects, result.js, 1024 * 1024)
                if len(final) != result.size:
                    raise ValueError("Saved final JavaScript size differs")
                expected = {"available": True, "output": final.decode("utf-8"), "saved_result": result.model_dump()}
            elif result.failure_reason == "generation_failed":
                expected = {"available": True, "output": None, "saved_result": result.model_dump()}
            else:
                expected["reason"] = result.failure_reason or "missing_saved_output"
        (directory / "expected.json").write_bytes(json_bytes(expected))
        prompts.append({"id": prompt.id, "sha256": prompt.sha256})
    inventory = export_requests(
        objects,
        scope,
        output / "bundle",
        pricing,
        emit,
        storage_prefix=inputs.storage_prefix,
        index_cache=index_cache,
        refresh_index=refresh_index,
    )
    manifest = PreparedReplay.model_validate(
        {
            "version": 1,
            "competition_repository": git.repo,
            "competition_ref": ref,
            "round": full_round,
            "hotkey": hotkey,
            "source_repository": submission.repo,
            "source_commit": submission.commit,
            "storage_prefix": inputs.storage_prefix,
            "seed": seed,
            "prompts": prompts,
            "providers": sorted({c.call.request.provider for c in inventory.captures.values()}),
            "historical_observations_available": False,
        }
    )
    (output / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    return manifest
