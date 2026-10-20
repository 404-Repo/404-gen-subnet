"""Python HTTP gateway and bounded WebSocket transport for ordinary CPU Pods."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from aiohttp import WSMsgType, web
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from witness_client.calls import Digest, Generation, Invocation
from witness_client.request import PROVIDERS

from .admission import CapacityReached
from .auth import Authenticator, AuthRejected
from .budget import BudgetRejected
from .control import Peer, Registry
from .lifecycle import AllocationConflict, Allocator, InventoryConflict
from .models import Scope, identifier, object_dict
from .records import Call, CallRecords, RecordingConflict
from .runpod import ProviderUnavailable
from .security import capability, capture_token, sealed
from .state import StateConflict, WriteUncertain


class Evidence(Protocol):
    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None: ...


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Start(StrictInput):
    round: str
    start_id: str
    replaces: str | None = None


class Dispatch(StrictInput):
    invocation: Invocation
    start_id: str


class Finish(StrictInput):
    generation: Generation
    count: int = Field(ge=0, le=4096)
    output_sha256: Digest
    output_is_none: bool


@dataclass
class Bridge:
    worker: web.WebSocketResponse
    session: dict[str, object] | None = None
    miner: web.WebSocketResponse | None = None
    claimed: bool = False


class Gateway:
    def __init__(
        self,
        *,
        allocator: Allocator,
        records: CallRecords,
        auth: Authenticator,
        registry: Registry,
        evidence: Evidence,
        deployment: str,
        round_number: str,
        round_end: int,
        secret: bytes,
        proof_hash: str,
        prompts: frozenset[str],
        fence: Callable[[], bool],
        pricing_hash: str,
    ) -> None:
        self.allocator, self.records, self.auth = allocator, records, auth
        self.registry, self.evidence = registry, evidence
        self.deployment, self.round, self.round_end = (
            deployment,
            round_number,
            round_end,
        )
        self.secret, self.proof_hash, self.prompts, self.fence = (
            secret,
            proof_hash,
            prompts,
            fence,
        )
        self.pricing_hash = pricing_hash
        self.bridges: dict[str, Bridge] = {}
        self.downloads = asyncio.Semaphore(8)
        self.provisioning = asyncio.Semaphore(4)

    def scope(self, hotkey: str) -> Scope:
        return Scope(self.deployment, self.round, hotkey)

    @web.middleware
    async def middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            if not await asyncio.to_thread(self.fence):
                raise AuthRejected("gateway_fenced", 503)
            if request.query_string or request.raw_path != request.path:
                raise AuthRejected("unknown_route", 404)
            if request.path != "/health" and not request.path.startswith(
                ("/control/", "/worker/")
            ):
                body = await request.read()
                if len(body) > 16384 or (request.method == "GET" and body):
                    raise ValueError("Invalid body size")
                for name in (
                    "x-bittensor-version",
                    "x-bittensor-hotkey",
                    "x-bittensor-nonce",
                    "x-bittensor-signature",
                    "x-bittensor-receiver",
                ):
                    if len(request.headers.getall(name, [])) != 1:
                        raise AuthRejected("invalid_hotkey_auth")
                request["hotkey"] = await asyncio.to_thread(
                    self.auth.verify,
                    request.method,
                    request.path,
                    body,
                    request.headers,
                )
            return await handler(request)
        except AuthRejected as error:
            return web.json_response(
                {"error": str(error)},
                status=error.status,
                headers={"Cache-Control": "no-store"},
            )
        except CapacityReached:
            return web.json_response(
                {"error": "capacity_reached"}, status=429, headers={"Retry-After": "15"}
            )
        except (AllocationConflict, InventoryConflict, RecordingConflict) as error:
            return web.json_response({"error": type(error).__name__}, status=409)
        except (ValidationError, ValueError, KeyError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except (StateConflict, WriteUncertain, ProviderUnavailable, BudgetRejected):
            return web.json_response(
                {"error": "control_plane_unavailable"},
                status=503,
                headers={"Retry-After": "5"},
            )
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - never expose provider/SDK diagnostics
            # Do not expose SDK diagnostics, environment variables, or provider errors.
            return web.json_response({"error": "control_plane_unavailable"}, status=503)

    def _scope(self, request: web.Request) -> Scope:
        return self.scope(str(request["hotkey"]))

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ready", "transport": "pods-v1", "receiver": "404-witness"}
        )

    async def current(self, request: web.Request) -> web.Response:
        current = await asyncio.to_thread(self.allocator.current, self._scope(request))
        return web.json_response(
            {
                "session": None if current is None else current.to_dict(),
                "round": self.round,
            }
        )

    async def start(self, request: web.Request) -> web.Response:
        if time.time() >= self.round_end:
            raise AuthRejected("round_closed", 403)
        data = Start.model_validate_json(await request.read())
        if data.round != self.round:
            raise AuthRejected("round_changed", 409)
        async with self.provisioning:
            session = await asyncio.to_thread(
                self.allocator.acquire,
                self._scope(request),
                data.start_id,
                replaces=data.replaces,
            )
        return web.json_response(
            {"session": session.to_dict(), "round": self.round},
            status=200 if session.phase == "allocated" else 202,
        )

    async def pod_status(self, request: web.Request) -> web.Response:
        scope, start_id = (
            self._scope(request),
            identifier(request.match_info["start_id"]),
        )
        session = await asyncio.to_thread(self.allocator.status, scope, start_id)
        if session is None:
            raise AuthRejected("session_not_found", 404)
        if session.phase in ("creating", "draining", "retiring"):
            async with self.provisioning:
                await asyncio.to_thread(self.allocator.reconcile, scope)
            session = await asyncio.to_thread(self.allocator.status, scope, start_id)
        assert session is not None
        return web.json_response(
            {
                "session": session.to_dict(),
                "ready": session.pod_id is not None
                and self.registry.ready(session.pod_id),
            }
        )

    def _check_generation(self, invocation: Invocation) -> None:
        if invocation.generation.round != self.round:
            raise AuthRejected("round_changed", 409)
        if invocation.generation.prompt_sha256 not in self.prompts:
            raise AuthRejected("unknown_round_prompt", 403)

    async def _snapshot(self, scope: Scope, call: Call) -> dict[str, object]:
        bridge = self.bridges.get(call.exchange_id)
        session = bridge.session if bridge is not None and not bridge.claimed else None
        return {
            "call": call.model_dump(exclude={"capture_id"}),
            "round": scope.round,
            "hotkey": scope.hotkey,
            "session": session,
        }

    async def reserve(self, request: web.Request) -> web.Response:
        invocation = Invocation.model_validate_json(await request.read())
        self._check_generation(invocation)
        scope = self._scope(request)
        if time.time() >= self.round_end:
            recording = await asyncio.to_thread(
                self.records.load, scope, invocation.generation.id
            )
            if recording is None or invocation.ordinal >= len(recording.entries):
                raise AuthRejected("round_closed", 403)
        call = await asyncio.to_thread(self.records.reserve, scope, invocation)
        return web.json_response(await self._snapshot(scope, call))

    async def dispatch(self, request: web.Request) -> web.Response:
        data = Dispatch.model_validate_json(await request.read())
        invocation, scope = data.invocation, self._scope(request)
        self._check_generation(invocation)
        located = await asyncio.to_thread(
            self.records.locate, scope, self._exchange_id(scope, invocation)
        )
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        _, call = located
        if invocation.ordinal != call.owner_ordinal:
            raise RecordingConflict("Only the original invocation dispatches")
        if call.phase in ("issued", "failed", "capturing"):
            return web.json_response(await self._snapshot(scope, call))
        if time.time() >= self.round_end:
            raise AuthRejected("round_closed", 403)
        current = await asyncio.to_thread(self.allocator.current, scope)
        if (
            current is None
            or current.start_id != data.start_id
            or current.phase != "allocated"
            or current.pod_id is None
        ):
            raise AllocationConflict("Pod is not the current allocation")
        if not self.registry.ready(current.pod_id):
            return web.json_response({"error": "pod_starting"}, status=503)
        reassign = False
        if call.phase == "assigned" and call.pod_id != current.pod_id:
            assert call.pod_id is not None
            reassign = (
                await asyncio.to_thread(self.allocator.provider.get, call.pod_id)
                is None
            )
        call = await asyncio.to_thread(
            self.records.assign,
            scope,
            invocation,
            current.start_id,
            current.pod_id,
            allow_reassign=reassign,
        )
        if call.pod_id == current.pod_id and call.phase == "assigned":
            await self.registry.command(
                current.pod_id,
                "capture",
                invocation=invocation.model_dump(),
                call=call.model_dump(),
            )
        return web.json_response(await self._snapshot(scope, call), status=202)

    @staticmethod
    def _exchange_id(scope: Scope, invocation: Invocation) -> str:
        import uuid

        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{scope.key}/{invocation.generation.id}/{invocation.request.id}",
            )
        )

    async def exchange_status(self, request: web.Request) -> web.Response:
        scope = self._scope(request)
        located = await asyncio.to_thread(
            self.records.locate, scope, identifier(request.match_info["exchange_id"])
        )
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        return web.json_response(await self._snapshot(scope, located[1]))

    async def deliver(self, request: web.Request) -> web.Response:
        invocation = Invocation.model_validate_json(await request.read())
        delivery = await asyncio.to_thread(
            self.records.deliver, self._scope(request), invocation
        )
        return web.json_response({"delivery": delivery})

    async def release(self, request: web.Request) -> web.Response:
        scope = self._scope(request)
        exchange_id = identifier(request.match_info["exchange_id"])
        located = await asyncio.to_thread(self.records.locate, scope, exchange_id)
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        await asyncio.to_thread(self.records.release, scope, located[0])
        bridge = self.bridges.get(exchange_id)
        if bridge is not None and bridge.miner is not None:
            await bridge.miner.close()
        return web.json_response({"released": True})

    async def finish(self, request: web.Request) -> web.Response:
        data = Finish.model_validate_json(await request.read())
        if data.generation.round != self.round:
            raise AuthRejected("round_changed", 409)
        await asyncio.to_thread(
            self.records.finish_digest,
            self._scope(request),
            data.generation,
            data.count,
            data.output_sha256,
            data.output_is_none,
        )
        return web.json_response({"finished": True})

    async def recording(self, request: web.Request) -> web.Response:
        recording = await asyncio.to_thread(
            self.records.load, self._scope(request), request.match_info["generation_id"]
        )
        if recording is None:
            raise AuthRejected("recording_not_found", 404)
        data = recording.model_dump()
        data["calls"] = {
            key: call.model_dump(exclude={"capture_id"})
            for key, call in recording.calls.items()
        }
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def artifact(self, request: web.Request) -> web.Response:
        scope, exchange_id, name = (
            self._scope(request),
            identifier(request.match_info["exchange_id"]),
            request.match_info["name"],
        )
        located = await asyncio.to_thread(self.records.locate, scope, exchange_id)
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        call = located[1]
        if name == "progress":
            if call.phase == "failed":
                return web.json_response(
                    {
                        "status": "failed",
                        "authorized": call.authorized,
                        "error_type": call.failure,
                    }
                )
            name = "status.json"
        allowed = {
            "status.json",
            "session.json",
            "request.json",
            "manifest.json",
            "response.sse",
            "client.bin",
            "server.bin",
            "authorization-server.bin",
            "claim.json",
            "authorization.json",
            "handshake.json",
        }
        if name not in allowed:
            raise AuthRejected("unknown_route", 404)
        if (
            name in ("response.sse", "manifest.json", "request.json")
            and call.phase != "issued"
        ):
            raise AuthRejected("object_not_ready", 404)
        async with self.downloads:
            data = await asyncio.to_thread(
                self.evidence.read,
                f"rounds/{scope.round}/{scope.hotkey}/{exchange_id}/{name}",
            )
            if data is None:
                if name == "status.json":
                    return web.json_response(
                        {"status": "running", "authorized": call.authorized}
                    )
                raise AuthRejected("object_not_ready", 404)
            return web.Response(
                body=data, headers={"Cache-Control": "private, no-store"}
            )

    async def pod_control(self, request: web.Request) -> web.WebSocketResponse:
        scope = self.scope(request.match_info["hotkey"])
        start_id, pod_id = (
            identifier(request.match_info["start_id"]),
            request.headers.get("x-witness-pod", ""),
        )
        token = capability(self.secret, scope, start_id)
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + token
        ):
            raise AuthRejected("invalid_pod_capability")
        if not await asyncio.to_thread(
            self.allocator.selected, scope, start_id, pod_id
        ):
            raise AuthRejected("pod_not_selected", 409)
        pod = await asyncio.to_thread(self.allocator.provider.get, pod_id)
        current = await asyncio.to_thread(self.allocator.status, scope, start_id)
        if pod is None or current is None or not pod.matches(scope, current):
            raise AuthRejected("pod_identity_changed", 409)
        if self.allocator.admission is not None and (
            pod.hourly_cost is None
            or not Decimal(pod.hourly_cost).is_finite()
            or Decimal(pod.hourly_cost) > self.allocator.admission.per_pod
        ):
            from .security import seal

            await asyncio.to_thread(
                seal,
                self.records.store,
                scope,
                start_id,
                pod_id,
                token,
                "request-drain",
            )
            raise AuthRejected("pod_price_above_ceiling", 409)
        if await asyncio.to_thread(
            sealed, self.records.store, scope, start_id, pod_id, token
        ):
            raise AuthRejected("pod_drained", 409)
        if pod_id in self.registry.peers:
            raise AuthRejected("pod_already_connected", 409)
        socket = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
        await socket.prepare(request)
        peer = Peer(socket, scope, start_id, pod_id)
        self.registry.peers[pod_id] = peer
        try:
            await socket.send_json({"type": "activate", "pod_id": pod_id})
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    break
                data = object_dict(json.loads(message.data))
                peer.seen = time.monotonic()
                if data.get("type") in ("ready", "heartbeat"):
                    peer.ready = (
                        data.get("ready") is True
                        and data.get("proof_key_sha256") == self.proof_hash
                        and data.get("pricing_sha256") == self.pricing_hash
                    )
                elif data.get("type") == "reply":
                    pending = peer.replies.get(str(data.get("id")))
                    if pending is not None and not pending.done():
                        pending.set_result(data)
                elif data.get("type") == "drained":
                    peer.ready = False
                    current = await asyncio.to_thread(self.allocator.current, scope)
                    if (
                        current is not None
                        and current.start_id == start_id
                        and current.pod_id == pod_id
                        and current.phase in ("allocated", "retiring")
                    ):
                        await asyncio.to_thread(
                            self.allocator.retire, scope, start_id, pod_id
                        )
                else:
                    break
        finally:
            peer.ready = False
            if self.registry.peers.get(pod_id) is peer:
                self.registry.peers.pop(pod_id, None)
            for future in peer.replies.values():
                if not future.done():
                    future.set_result({})
            await socket.close()
        return socket

    async def worker_exchange(self, request: web.Request) -> web.WebSocketResponse:
        scope = self.scope(request.headers.get("x-witness-hotkey", ""))
        located = await asyncio.to_thread(
            self.records.locate, scope, identifier(request.match_info["exchange_id"])
        )
        if located is None:
            raise AuthRejected("exchange_not_found", 404)
        invocation, call = located
        if call.start_id is None or call.pod_id is None or call.capture_id is None:
            raise AuthRejected("capture_not_assigned", 409)
        token = capture_token(
            capability(self.secret, scope, call.start_id),
            call.exchange_id,
            call.capture_id,
        )
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + token
        ):
            raise AuthRejected("invalid_capture_capability")
        if not await asyncio.to_thread(
            self.allocator.selected, scope, call.start_id, call.pod_id
        ):
            raise AuthRejected("pod_not_selected", 409)
        if not await asyncio.to_thread(
            self.records.claim, scope, invocation, call.pod_id, call.capture_id
        ):
            raise AuthRejected("capture_already_claimed", 409)
        socket = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=15)
        await socket.prepare(request)
        bridge = Bridge(socket)
        self.bridges[call.exchange_id] = bridge
        received = 0
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    data = object_dict(json.loads(message.data))
                    if data.get("type") == "ready":
                        session = object_dict(data.get("session"))
                        host, path = PROVIDERS[invocation.request.provider]
                        if bridge.session is not None or any(
                            session.get(k) != v
                            for k, v in {
                                "id": call.exchange_id,
                                "protocol": 2,
                                "provider": f"https://{host}{path}",
                                "proof_key_sha256": self.proof_hash,
                                "round": scope.round,
                                "miner": scope.hotkey,
                            }.items()
                        ):
                            raise ValueError("Invalid witness session")
                        bridge.session = session
                        continue
                    if data.get("type") == "finished":
                        break
                    if (
                        data.get("type") not in ("status", "end")
                        or len(message.data.encode()) > 4096
                    ):
                        raise ValueError("Invalid worker control message")
                    if bridge.miner is not None:
                        await bridge.miner.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    received += len(message.data)
                    if (
                        not message.data
                        or len(message.data) > 256 * 1024
                        or received > 16 * 1024 * 1024
                    ):
                        raise ValueError("Invalid worker traffic")
                    if bridge.miner is not None:
                        await bridge.miner.send_bytes(message.data)
                else:
                    break
        finally:
            self.bridges.pop(call.exchange_id, None)
            if bridge.miner is not None:
                await bridge.miner.close()
            await socket.close()
        return socket

    async def miner_exchange(self, request: web.Request) -> web.WebSocketResponse:
        scope, exchange_id = (
            self._scope(request),
            identifier(request.match_info["exchange_id"]),
        )
        located = await asyncio.to_thread(self.records.locate, scope, exchange_id)
        bridge = self.bridges.get(exchange_id)
        if (
            located is None
            or bridge is None
            or bridge.session is None
            or bridge.claimed
        ):
            raise AuthRejected("exchange_not_connectable", 409)
        bridge.claimed = True
        miner = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=15)
        await miner.prepare(request)
        bridge.miner = miner
        received = messages = 0
        try:
            async for message in miner:
                messages += 1
                if messages > 4096:
                    raise ValueError("Too many transport messages")
                if message.type == WSMsgType.BINARY:
                    received += len(message.data)
                    if (
                        not message.data
                        or len(message.data) > 256 * 1024
                        or received > 16 * 1024 * 1024
                    ):
                        raise ValueError("Invalid client traffic")
                    await bridge.worker.send_bytes(message.data)
                elif message.type == WSMsgType.TEXT:
                    await bridge.worker.send_str(message.data)
                else:
                    break
        finally:
            bridge.miner = None
            await bridge.worker.close()
            await miner.close()
        return miner

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=16384)
        app.add_routes(
            [
                web.get("/health", self.health),
                web.get("/pods/current", self.current),
                web.post("/pods/start", self.start),
                web.get("/pods/{start_id}", self.pod_status),
                web.post("/calls", self.reserve),
                web.post("/calls/dispatch", self.dispatch),
                web.post("/calls/deliver", self.deliver),
                web.post("/generations/finish", self.finish),
                web.post("/release/{exchange_id}", self.release),
                web.get("/recordings/{generation_id}", self.recording),
                web.get("/status/{exchange_id}", self.exchange_status),
                web.get("/exchanges/{exchange_id}/connect", self.miner_exchange),
                web.get("/exchanges/{exchange_id}/{name}", self.artifact),
                web.get("/control/{hotkey}/{start_id}", self.pod_control),
                web.get("/worker/exchanges/{exchange_id}", self.worker_exchange),
            ]
        )
        return app
