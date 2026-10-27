"""Authenticated per-miner call service on the witness Pod's direct TLS port."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import queue
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx
from aiohttp import WSMsgType, web
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from renderer_client import RenderRejected
from subnet_common.competition.generations import GenerationsAdapter
from witness_client.calls import RequestKey
from witness_client.request import PROVIDERS

from .artifacts import ArtifactRepository, SubmissionContext
from .auth import Authenticator, AuthRejected, Membership
from .models import Scope, identifier
from .records import Call, RecordingConflict
from .recovery import recover_issued
from .results import ResultSaver
from .security import verify_miner_access

if TYPE_CHECKING:
    from pod import PodRuntime

SAVE_QUEUE_CAPACITY = 8
SAVE_QUEUE_WAIT_SECONDS = 120


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Start(StrictInput):
    request: RequestKey
    expected_exchange_id: str | None = None


class SaveChoice(StrictInput):
    generation: SubmissionContext
    source: str | None = Field(max_length=1024 * 1024)
    prompt_image: str | None = Field(default=None, max_length=12 * 1024 * 1024)


class AssignedMiner(Membership):
    def __init__(self, hotkey: str) -> None:
        self.hotkey = hotkey

    def require(self, hotkey: str) -> None:
        if hotkey != self.hotkey:
            raise AuthRejected("miner_not_assigned", 403)


class Witness:
    def __init__(
        self,
        runtime: PodRuntime,
        artifact_url: Callable[[str], str],
        *,
        artifacts: ArtifactRepository | None = None,
        result_saver: ResultSaver | None = None,
    ) -> None:
        self.runtime, self.artifact_url = runtime, artifact_url
        self.records, self.budget, self.evidence = runtime.records, runtime.budget, runtime.evidence
        self.round, self.round_end, self.proof_hash = runtime.scope.round, runtime.round_end, runtime.proof_hash
        self.auth = Authenticator(AssignedMiner(runtime.scope.hotkey))
        self.renderer_url = "http://127.0.0.1:8002/render/grid"
        if artifacts is not None and artifacts.scope != runtime.scope:
            raise ValueError("Artifact storage belongs to another miner")
        self.artifacts, self.result_saver = artifacts, result_saver
        self._save_lock = asyncio.Lock()
        self._pending_saves = 0

    @web.middleware
    async def middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            if request.raw_path != request.path and not request.path.startswith("/v1/render/"):
                raise AuthRejected("unknown_route", 404)
            body = await request.read()
            if request.method == "GET" and body:
                raise ValueError("Unexpected request body")
            if not (request.path.startswith("/v1/render/") or request.path == "/v1/submissions") and len(body) > 16384:
                raise ValueError("Invalid body size")
            for name in (
                "x-bittensor-version",
                "x-bittensor-hotkey",
                "x-bittensor-nonce",
                "x-bittensor-signature",
                "x-bittensor-receiver",
                "authorization",
            ):
                if len(request.headers.getall(name, [])) != 1:
                    raise AuthRejected("invalid_hotkey_auth")
            self.auth.verify(request.method, request.raw_path, body, request.headers)
            if not verify_miner_access(
                request.headers.get("Authorization", "").removeprefix("Bearer "),
                self.runtime.token,
                self.runtime.scope,
                self.runtime.start_id,
                self.runtime.pod_id,
            ):
                raise AuthRejected("allocation_access_expired", 401)
            return await handler(request)
        except AuthRejected as error:
            return web.json_response({"error": str(error)}, status=error.status, headers={"Cache-Control": "no-store"})
        except RecordingConflict:
            return web.json_response({"error": "RecordingConflict"}, status=409)
        except RenderRejected as error:
            return web.Response(text=error.detail_json, status=422, content_type="application/json")
        except (ValidationError, ValueError, KeyError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - do not expose credentials or SDK diagnostics
            return web.json_response({"error": "witness_unavailable"}, status=503)

    def _scope(self, request: web.Request) -> Scope:
        return self.runtime.scope

    def _require_selected(self) -> None:
        if not self.runtime.selected():
            raise AuthRejected("pod_not_selected", 409)

    async def _snapshot(self, scope: Scope, invocation: RequestKey, call: Call) -> dict[str, object]:
        call = await asyncio.to_thread(
            recover_issued, self.records, self.budget, self.evidence.get, scope, invocation, call, self.proof_hash
        )
        session = self.session(invocation, call) if call.phase == "assigned" else None
        completion = (
            await asyncio.to_thread(self.completion, scope, invocation, call) if call.phase == "issued" else None
        )
        return {
            "call": call.model_dump(exclude={"capture_id"}),
            "hotkey": scope.hotkey,
            "session": session,
            "completion": completion,
        }

    async def _call_response(self, scope: Scope, invocation: RequestKey, call: Call) -> web.Response:
        snapshot = await self._snapshot(scope, invocation, call)
        pending = snapshot["completion"] is None
        headers = {"Cache-Control": "no-store", "Location": f"/v1/calls/{call.exchange_id}"}
        if pending:
            headers["Retry-After"] = "1"
        return web.json_response(snapshot, status=202 if pending else 200, headers=headers)

    async def start(self, request: web.Request) -> web.Response:
        data = Start.model_validate_json(await request.read())
        invocation = data.request
        expected = identifier(data.expected_exchange_id) if data.expected_exchange_id is not None else None
        scope = self._scope(request)
        record = await asyncio.to_thread(self.records.load, scope, invocation.id)
        call = None if record is None else record.call
        if call is None and expected is not None:
            raise AuthRejected("exchange_not_found", 404)
        if call is not None:
            if call.request != invocation:
                raise RecordingConflict("Call identity differs")
            if call.phase in ("issued", "capturing") or (
                call.phase == "failed" and (
                    expected != call.exchange_id or call.authorized or not call.not_dispatched
                )
            ):
                return await self._call_response(scope, invocation, call)
        if time.time() >= self.round_end:
            raise AuthRejected("round_closed", 403)
        await asyncio.to_thread(self._require_selected)
        call = await asyncio.to_thread(
            self.records.assign,
            scope,
            invocation,
            self.runtime.start_id,
            self.runtime.pod_id,
            allow_reassign=True,
            expected_exchange_id=expected,
        )
        return await self._call_response(scope, invocation, call)

    async def exchange_status(self, request: web.Request) -> web.Response:
        scope = self._scope(request)
        located = await asyncio.to_thread(self.records.locate, scope, identifier(request.match_info["exchange_id"]))
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        return web.json_response(await self._snapshot(scope, *located))

    def session(self, invocation: RequestKey, call: Call) -> dict[str, object]:
        host, path = PROVIDERS[invocation.provider]
        return {
            "id": call.exchange_id,
            "protocol": 2,
            "provider": f"https://{host}{path}",
            "proof_key_sha256": self.proof_hash,
            "round": self.round,
            "miner": self.runtime.scope.hotkey,
        }

    def link(self, key: str, raw: bytes) -> dict[str, object]:
        return {"url": self.artifact_url(key), "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    def completion(self, scope: Scope, invocation: RequestKey, call: Call) -> dict[str, object]:
        prefix = f"rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
        manifest = self.evidence.get(prefix + "manifest.json", 1024 * 1024)
        if manifest is None or hashlib.sha256(manifest).hexdigest() != call.manifest_sha256:
            raise RecordingConflict("Durable manifest differs")
        metadata = json.loads(manifest)
        objects = metadata.get("artifacts")
        if not isinstance(objects, dict):
            raise RecordingConflict("Manifest has no artifact descriptors")
        links = {}
        for name, item in objects.items():
            if name not in self.runtime.PUBLIC_EVIDENCE or not isinstance(item, dict):
                raise RecordingConflict("Invalid artifact descriptor")
            digest, size = item["sha256"], item["size"]
            key = self.runtime.artifact_key(call.exchange_id, name, digest)
            if item["key"] != key:
                raise RecordingConflict("Artifact is outside the exchange")
            links[name] = {"url": self.artifact_url(key), "sha256": digest, "size": size}
        key = self.runtime.artifact_key(call.exchange_id, "manifest.json", call.manifest_sha256 or "")
        self.evidence.put(key, manifest)
        links["manifest.json"] = self.link(key, manifest)
        return {"manifest": manifest.decode(), "session": self.session(invocation, call), "artifacts": links}

    async def miner_exchange(self, request: web.Request) -> web.WebSocketResponse:
        from channel import WorkerChannel

        await asyncio.to_thread(self._require_selected)
        exchange_id = identifier(request.match_info["exchange_id"])
        located = await asyncio.to_thread(self.records.locate, self.runtime.scope, exchange_id)
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        invocation, call = located
        if call.phase != "assigned" or call.start_id != self.runtime.start_id or call.pod_id != self.runtime.pod_id:
            raise AuthRejected("exchange_not_connectable", 409)
        socket = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=15)
        await socket.prepare(request)
        channel_socket = DirectSocket(socket)
        if not await asyncio.to_thread(self.runtime.start_capture, invocation, call, WorkerChannel(channel_socket)):
            await socket.close()
            return socket
        capture = self.runtime.active.get(exchange_id)
        try:
            async for message in socket:
                if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    break
                while True:
                    try:
                        channel_socket.incoming.put_nowait(message.data)
                        break
                    except queue.Full:
                        if capture is None or capture.done() or socket.closed:
                            return socket
                        await asyncio.sleep(0.01)
        finally:
            channel_socket.disconnected.set()
            await socket.close()
        return socket

    async def render(self, request: web.Request) -> web.Response:
        def admit() -> None:
            with self.runtime.lock:
                self._require_selected()
                if time.time() >= self.round_end:
                    raise AuthRejected("round_closed", 403)
                self.runtime.rendering += 1

        await asyncio.to_thread(admit)
        try:
            async with (
                httpx.AsyncClient(timeout=150, trust_env=False) as client,
                client.stream(
                    "POST",
                    self.renderer_url,
                    params=request.query,
                    content=await request.read(),
                    headers={"Content-Type": "application/json"},
                ) as response,
            ):
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 32 * 1024 * 1024:
                        raise ValueError("Renderer response exceeds limit")
                return web.Response(
                    body=bytes(body),
                    status=response.status_code,
                    headers={
                        "Content-Type": response.headers.get("Content-Type", "application/octet-stream"),
                        "Cache-Control": "no-store",
                    },
                )
        finally:
            with self.runtime.lock:
                self.runtime.rendering -= 1
                self.runtime.last_used = self.runtime.clock()

    def _artifacts(self, generation: SubmissionContext | None = None) -> ArtifactRepository:
        if self.artifacts is None:
            raise AuthRejected("artifact_storage_disabled", 404)
        if generation is not None:
            if generation.prompt_sha256 not in self.artifacts.prompt_ids:
                raise AuthRejected("unknown_round_prompt", 403)
        return self.artifacts

    def _admit_artifact(self) -> None:
        with self.runtime.lock:
            self._require_selected()
            if time.time() >= self.round_end:
                raise AuthRejected("round_closed", 403)
            self.runtime.rendering += 1

    def _artifact_done(self) -> None:
        with self.runtime.lock:
            self.runtime.rendering -= 1
            self.runtime.last_used = self.runtime.clock()

    async def artifact_context(self, request: web.Request) -> web.Response:
        store = self._artifacts()
        return web.json_response(
            {"submission_commit": store.submission_commit}, headers={"Cache-Control": "no-store"}
        )

    async def save_choice(self, request: web.Request) -> web.Response:
        value = json.loads(await request.read())
        store = self._artifacts()
        if isinstance(value, dict) and isinstance(value.get("generation"), dict):
            value["generation"].setdefault("seed", store.seed)
        data = SaveChoice.model_validate(value)
        self._artifacts(data.generation)
        store.require(data.generation)
        image = None
        if data.source is not None:
            if data.prompt_image is None:
                raise ValueError("Final source requires the prompt image")
            image = base64.b64decode(data.prompt_image, validate=True)
            if len(image) > 8 * 1024 * 1024 or hashlib.sha256(image).hexdigest() != data.generation.prompt_sha256:
                raise ValueError("Prompt bytes differ")
        if self.result_saver is None:
            raise AuthRejected("saving_unavailable", 503)
        if self._pending_saves >= SAVE_QUEUE_CAPACITY + 1:
            raise AuthRejected("save_queue_full", 503)
        self._pending_saves += 1
        try:
            try:
                async with asyncio.timeout(SAVE_QUEUE_WAIT_SECONDS):
                    await self._save_lock.acquire()
            except TimeoutError as error:
                raise AuthRejected("save_queue_wait_timeout", 503) from error
            try:
                previous = await asyncio.to_thread(store.result, data.generation.prompt_sha256)
                if previous is not None:
                    if data.source is None:
                        same = previous.js is None
                    else:
                        same = previous.js is not None and await asyncio.to_thread(
                            store.objects.get, previous.js, 1024 * 1024
                        ) == data.source.encode()
                    if not same:
                        raise RecordingConflict("Final output already saved")
                    return web.json_response(
                        {"submission": previous.model_dump()}, headers={"Cache-Control": "no-store"}
                    )
                await asyncio.to_thread(self._admit_artifact)
                try:
                    result = await self.result_saver.save(data.generation.prompt_sha256, data.source, image)
                    return web.json_response(
                        {"submission": result.model_dump()}, headers={"Cache-Control": "no-store"}
                    )
                finally:
                    self._artifact_done()
            finally:
                self._save_lock.release()
        finally:
            self._pending_saves -= 1

    async def get_choices(self, request: web.Request) -> web.Response:
        store = self._artifacts()
        results = await asyncio.to_thread(store.results)
        return web.Response(
            body=b'{"submissions":' + GenerationsAdapter.dump_json(results) + b"}",
            content_type="application/json", headers={"Cache-Control": "no-store"},
        )

    async def get_choice(self, request: web.Request) -> web.Response:
        store = self._artifacts()
        result = await asyncio.to_thread(store.result, request.match_info["prompt"])
        if result is None:
            raise AuthRejected("submission_not_found", 404)
        return web.json_response({"submission": result.model_dump()}, headers={"Cache-Control": "no-store"})

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=16 * 1024 * 1024 + 4096)
        app.add_routes(
            [
                web.post("/v1/calls", self.start),
                web.get("/v1/calls/{exchange_id}", self.exchange_status),
                web.get("/v1/exchanges/{exchange_id}/connect", self.miner_exchange),
                web.post("/v1/render/grid", self.render),
                web.get("/v1/artifacts/context", self.artifact_context),
                web.post("/v1/submissions", self.save_choice),
                web.get("/v1/submissions", self.get_choices),
                web.get("/v1/submissions/{prompt}", self.get_choice),
            ]
        )
        return app


class DirectSocket:
    """Bounded ingress; a paid capture outlives the miner's HTTP handler."""

    def __init__(self, socket: web.WebSocketResponse) -> None:
        import threading

        self.socket, self.loop = socket, asyncio.get_running_loop()
        self.incoming: queue.Queue[str | bytes] = queue.Queue(maxsize=16)
        self.disconnected = threading.Event()

    def recv(self, timeout: float | None = None) -> str | bytes:
        deadline = time.monotonic() + (timeout if timeout is not None else 1800)
        while True:
            if self.disconnected.is_set() and self.incoming.empty():
                raise OSError("Miner disconnected")
            try:
                return self.incoming.get(timeout=max(0, min(0.05, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Waiting for miner") from None

    def send(self, message: str | bytes) -> None:
        if self.disconnected.is_set():
            raise OSError("Miner disconnected")
        operation = self.socket.send_bytes(message) if isinstance(message, bytes) else self.socket.send_str(message)
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        try:
            future.result(timeout=30)
        except Exception as error:
            future.cancel()
            raise OSError("Miner delivery failed") from error

    def close(self) -> None:
        self.disconnected.set()
        asyncio.run_coroutine_threadsafe(self.socket.close(), self.loop)
