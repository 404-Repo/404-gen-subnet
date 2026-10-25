"""Authenticated per-miner call service on the witness Pod's direct TLS port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx
from aiohttp import WSMsgType, web
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from renderer_client import RenderOptions, RenderRejected
from witness_client.calls import Digest, RequestKey
from witness_client.request import PROVIDERS

from .artifacts import (
    MAX_PNG_BYTES,
    MAX_REJECTION_BYTES,
    ArtifactConflict,
    ArtifactFailed,
    ArtifactLimit,
    ArtifactPending,
    ArtifactRepository,
    RenderArtifact,
    SubmissionContext,
)
from .auth import Authenticator, AuthRejected, Membership
from .models import Scope, identifier
from .records import Call, RecordingConflict
from .recovery import recover_issued
from .security import verify_miner_access

if TYPE_CHECKING:
    from pod import PodRuntime


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Dispatch(StrictInput):
    request: RequestKey
    start_id: str


class Retry(StrictInput):
    request: RequestKey
    exchange_id: str


class SaveRender(StrictInput):
    generation: SubmissionContext
    submission_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source: str
    options: RenderOptions = RenderOptions()
    include_png: bool = True


class SaveChoice(StrictInput):
    generation: SubmissionContext
    submission_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_sha256: Digest | None
    expected_revision: Digest | None = None


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
    ) -> None:
        self.runtime, self.artifact_url = runtime, artifact_url
        self.records, self.budget, self.evidence = runtime.records, runtime.budget, runtime.evidence
        self.round, self.round_end, self.proof_hash = runtime.scope.round, runtime.round_end, runtime.proof_hash
        self.auth = Authenticator(AssignedMiner(runtime.scope.hotkey))
        self.renderer_url = "http://127.0.0.1:8002/render/grid"
        if artifacts is not None and artifacts.scope != runtime.scope:
            raise ValueError("Artifact storage belongs to another miner")
        self.artifacts = artifacts

    @web.middleware
    async def middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            if request.raw_path != request.path and not request.path.startswith("/render/"):
                raise AuthRejected("unknown_route", 404)
            body = await request.read()
            if request.method == "GET" and body:
                raise ValueError("Unexpected request body")
            if not (request.path.startswith("/render/") or request.path == "/artifacts/render") and len(body) > 16384:
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
        except ArtifactConflict:
            return web.json_response({"error": "artifact_conflict"}, status=409)
        except ArtifactLimit:
            return web.json_response({"error": "artifact_storage_limit"}, status=429)
        except ArtifactFailed:
            return web.json_response({"error": "artifact_recovery_exhausted"}, status=409)
        except ArtifactPending:
            return web.json_response({"error": "artifact_pending"}, status=503)
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

    async def reserve(self, request: web.Request) -> web.Response:
        invocation = RequestKey.model_validate_json(await request.read())
        scope = self._scope(request)
        if time.time() >= self.round_end:
            record = await asyncio.to_thread(self.records.load, scope, invocation.id)
            if record is None:
                raise AuthRejected("round_closed", 403)
        await asyncio.to_thread(self._require_selected)
        call = await asyncio.to_thread(self.records.reserve, scope, invocation)
        return web.json_response(await self._snapshot(scope, invocation, call))

    async def dispatch(self, request: web.Request) -> web.Response:
        data = Dispatch.model_validate_json(await request.read())
        invocation, scope = data.request, self._scope(request)
        record = await asyncio.to_thread(self.records.load, scope, invocation.id)
        call = None if record is None else record.call
        if call is None:
            raise AuthRejected("exchange_not_found", 404)
        if call.phase in ("issued", "failed", "capturing"):
            return web.json_response(await self._snapshot(scope, invocation, call))
        if time.time() >= self.round_end:
            raise AuthRejected("round_closed", 403)
        await asyncio.to_thread(self._require_selected)
        if data.start_id != self.runtime.start_id:
            raise AuthRejected("pod_not_selected", 409)
        call = await asyncio.to_thread(
            self.records.assign,
            scope,
            invocation,
            self.runtime.start_id,
            self.runtime.pod_id,
            allow_reassign=call.phase == "assigned" and call.pod_id != self.runtime.pod_id,
        )
        return web.json_response(await self._snapshot(scope, invocation, call), status=202)

    async def retry(self, request: web.Request) -> web.Response:
        data = Retry.model_validate_json(await request.read())
        if time.time() >= self.round_end:
            raise AuthRejected("round_closed", 403)
        scope = self._scope(request)
        await asyncio.to_thread(self._require_selected)
        call = await asyncio.to_thread(self.records.retry, scope, data.request, identifier(data.exchange_id))
        return web.json_response(await self._snapshot(scope, data.request, call))

    async def exchange_status(self, request: web.Request) -> web.Response:
        scope = self._scope(request)
        located = await asyncio.to_thread(self.records.locate, scope, identifier(request.match_info["exchange_id"]))
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        return web.json_response(await self._snapshot(scope, *located))

    async def release(self, request: web.Request) -> web.Response:
        scope = self._scope(request)
        exchange_id = identifier(request.match_info["exchange_id"])
        located = await asyncio.to_thread(self.records.locate, scope, exchange_id)
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        await asyncio.to_thread(self.records.release, scope, located[0])
        return web.json_response({"released": True})

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
            if generation.round != self.round:
                raise AuthRejected("round_changed", 409)
            if generation.prompt_sha256 not in self.artifacts.prompt_hashes:
                raise AuthRejected("unknown_round_prompt", 403)
        return self.artifacts

    def _admit_artifact(self) -> float:
        with self.runtime.lock:
            self._require_selected()
            if time.time() >= self.round_end:
                raise AuthRejected("round_closed", 403)
            self.runtime.rendering += 1
            return time.time()

    def _artifact_done(self) -> None:
        with self.runtime.lock:
            self.runtime.rendering -= 1
            self.runtime.last_used = self.runtime.clock()

    async def _saved_render_response(self, artifact: RenderArtifact, include_png: bool) -> web.Response:
        store = self._artifacts(artifact.generation)
        headers = {
            "Cache-Control": "no-store",
            "X-Artifact-Id": artifact.artifact_id,
            "X-Source-Sha256": artifact.source.sha256,
            "X-Renderer-Id": artifact.profile.image_digest,
        }
        if not include_png:
            return web.json_response({"artifact": artifact.model_dump(mode="json")}, headers=headers)
        if artifact.rejection_json is not None:
            return web.Response(
                text=artifact.rejection_json, status=422, content_type="application/json", headers=headers
            )
        assert artifact.png is not None
        png = await asyncio.to_thread(store.read_object, artifact.png, MAX_PNG_BYTES)
        headers["X-Png-Sha256"] = artifact.png.sha256
        return web.Response(body=png, content_type="image/png", headers=headers)

    async def save_render(self, request: web.Request) -> web.Response:
        data = SaveRender.model_validate_json(await request.read())
        store = self._artifacts(data.generation)
        if data.submission_commit != store.submission_commit:
            raise ArtifactConflict("Source submission changed")
        artifact_id = store.identity(data.generation, data.source, data.options)
        saved = await asyncio.to_thread(store.load_render, data.generation, artifact_id)
        if saved is not None:
            await asyncio.to_thread(store.release_reservation, saved)
            return await self._saved_render_response(saved, data.include_png)
        await asyncio.to_thread(self._admit_artifact)
        try:
            owner = await asyncio.to_thread(store.reserve, data.generation, data.source, data.options)
            async with (
                httpx.AsyncClient(timeout=150, trust_env=False) as client,
                client.stream(
                    "POST",
                    self.renderer_url,
                    params=data.options.query(),
                    json={"source": data.source},
                ) as response,
            ):
                body = bytearray()
                limit = MAX_REJECTION_BYTES if response.status_code == 422 else MAX_PNG_BYTES
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ArtifactPending("Renderer response exceeds limit")
                png, rejection = None, None
                if response.status_code == 422:
                    try:
                        rejection = RenderRejected(body.decode()).detail_json
                    except (UnicodeError, ValueError) as error:
                        raise ArtifactPending("Invalid renderer rejection") from error
                else:
                    response.raise_for_status()
                    if response.headers.get("Content-Type", "").split(";", 1)[0] != "image/png":
                        raise ArtifactPending("Renderer did not return PNG bytes")
                    png = bytes(body)
                    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ArtifactPending("Invalid renderer PNG")
            saved = await asyncio.to_thread(
                store.complete,
                data.generation,
                data.source,
                data.options,
                owner=owner,
                png=png,
                rejection_json=rejection,
            )
            await asyncio.to_thread(store.release_reservation, saved)
            return await self._saved_render_response(saved, data.include_png)
        finally:
            self._artifact_done()

    async def save_choice(self, request: web.Request) -> web.Response:
        data = SaveChoice.model_validate_json(await request.read())
        store = self._artifacts(data.generation)
        store._generation(data.generation)
        if data.submission_commit != store.submission_commit:
            raise ArtifactConflict("Source submission changed")
        previous = await asyncio.to_thread(store.load_final, data.generation.prompt_sha256)
        operation = store.operation(data.generation, data.source_sha256)
        if previous is not None and previous.operation_id == operation:
            return web.json_response({"submission": previous.model_dump(mode="json")})
        if (previous.revision if previous else None) != data.expected_revision:
            raise ArtifactConflict("Final choice changed")
        accepted_at = await asyncio.to_thread(self._admit_artifact)
        try:
            artifact = None
            if data.source_sha256 is not None:
                artifact = await asyncio.to_thread(store.find_render, data.generation, data.source_sha256)
                if artifact is None:
                    raise AuthRejected("source_not_saved", 404)
            saved = await asyncio.to_thread(
                store.save_final,
                data.generation,
                data.source_sha256,
                data.expected_revision,
                artifact,
                accepted_at=accepted_at,
                round_end=self.round_end,
            )
            return web.json_response(
                {"submission": saved.model_dump(mode="json")}, headers={"Cache-Control": "no-store"}
            )
        finally:
            self._artifact_done()

    async def get_choice(self, request: web.Request) -> web.Response:
        prompt = request.match_info["prompt"]
        store = self._artifacts()
        if prompt not in store.prompt_hashes:
            raise AuthRejected("unknown_round_prompt", 403)
        saved = await asyncio.to_thread(store.load_final, prompt)
        if saved is None:
            raise AuthRejected("submission_not_found", 404)
        return web.json_response({"submission": saved.model_dump(mode="json")}, headers={"Cache-Control": "no-store"})

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=6 * 1024 * 1024 + 4096)
        app.add_routes(
            [
                web.post("/calls", self.reserve),
                web.post("/calls/dispatch", self.dispatch),
                web.post("/calls/retry", self.retry),
                web.post("/release/{exchange_id}", self.release),
                web.get("/status/{exchange_id}", self.exchange_status),
                web.get("/exchanges/{exchange_id}/connect", self.miner_exchange),
                web.post("/render/grid", self.render),
                web.post("/artifacts/render", self.save_render),
                web.post("/submissions", self.save_choice),
                web.get("/submissions/{prompt}", self.get_choice),
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
