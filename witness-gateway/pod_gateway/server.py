"""Miner authentication, Pod allocation and bounded lifecycle control."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiohttp import WSMsgType, web
from pydantic import BaseModel, ConfigDict, ValidationError

from .admission import CapacityReached
from .auth import Authenticator, AuthRejected
from .control import Peer, Registry
from .lifecycle import AllocationConflict, Allocator, InventoryConflict
from .models import Scope, Session, identifier, object_dict
from .runpod import ProviderUnavailable
from .security import capability, miner_access
from .state import StateConflict, WriteUncertain


class Evidence(Protocol):
    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None: ...


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    round: str
    start_id: str
    replaces: str | None = None


class Gateway:
    def __init__(self, *, allocator: Allocator, auth: Authenticator, registry: Registry,
                 deployment: str, round_number: str, round_end: int, secret: bytes,
                 proof_hash: str, fence: Callable[[], bool], pricing_hash: str) -> None:
        self.allocator, self.auth, self.registry = allocator, auth, registry
        self.deployment, self.round, self.round_end = deployment, round_number, round_end
        self.secret, self.proof_hash, self.fence = secret, proof_hash, fence
        self.pricing_hash = pricing_hash
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
            if request.query_string or request.raw_path != request.path:
                raise AuthRejected("unknown_route", 404)
            if request.path != "/health" and not request.path.startswith(
                ("/control/",)
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
                request["hotkey"] = self.auth.verify(
                    request.method,
                    request.path,
                    body,
                    request.headers,
                )
            if not await asyncio.to_thread(self.fence):
                raise AuthRejected("gateway_fenced", 503)
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
        except (AllocationConflict, InventoryConflict) as error:
            return web.json_response({"error": type(error).__name__}, status=409)
        except (ValidationError, ValueError, KeyError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except (StateConflict, WriteUncertain, ProviderUnavailable):
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
        if current is not None and current.phase == "allocated":
            async with self.provisioning:
                current = await asyncio.to_thread(self.allocator.reconcile, self._scope(request))
        return web.json_response(
            {
                "session": None if current is None else current.to_dict(),
                "witness": await self._endpoint(self._scope(request), current),
                "round": self.round,
            }
        )

    async def start(self, request: web.Request) -> web.Response:
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
        if session.phase in ("creating", "draining", "retiring", "allocated"):
            async with self.provisioning:
                await asyncio.to_thread(self.allocator.reconcile, scope)
            session = await asyncio.to_thread(self.allocator.status, scope, start_id)
        assert session is not None
        return web.json_response(
            {
                "session": session.to_dict(),
                "witness": await self._endpoint(scope, session),
                "round": self.round,
                "ready": session.pod_id is not None
                and self.registry.ready(session.pod_id)
                and self.registry.peers[session.pod_id].certificate is not None,
            }
        )

    async def _endpoint(self, scope: Scope, session: Session | None) -> dict[str, object] | None:
        if session is None or session.phase != "allocated" or session.pod_id is None:
            return None
        peer = self.registry.peers.get(session.pod_id)
        if peer is None or not self.registry.ready(session.pod_id) or peer.certificate is None:
            return None
        pod = await asyncio.to_thread(self.allocator.provider.get, session.pod_id)
        if (pod is None or not pod.matches(scope, session) or pod.lifecycle != "active"
                or self.allocator.spec.port not in pod.tcp
                or not await asyncio.to_thread(self.allocator.selected, scope, session.start_id, session.pod_id)):
            return None
        if self.allocator.selection is None or not await asyncio.to_thread(
            self.allocator.selection.selected, scope, session.start_id, session.pod_id
        ):
            return None
        address, port = pod.tcp[self.allocator.spec.port]
        expires = int(time.time()) + 600
        return {
            "address": address, "port": port, "server_name": "witness.internal",
            "certificate": peer.certificate, "pod_id": session.pod_id, "start_id": session.start_id,
            "access": miner_access(capability(self.secret, scope, session.start_id), scope,
                                   session.start_id, session.pod_id, expires),
            "expires": expires,
        }

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
        pod = await asyncio.to_thread(self.allocator.provider.get, pod_id)
        if (
            pod is None
            or pod.scope() != scope
            or pod.markers["WITNESS_START_ID"] != start_id
        ):
            raise AuthRejected("pod_identity_changed", 409)
        if pod.lifecycle == "pending" or (
            pod.lifecycle == "active" and pod.status != "RUNNING"
        ):
            raise AuthRejected("pod_starting", 503)
        if pod.lifecycle == "drained":
            raise AuthRejected("pod_drained", 409)
        if pod_id in self.registry.peers:
            raise AuthRejected("pod_already_connected", 409)
        socket = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
        await socket.prepare(request)
        peer = Peer(socket, scope, start_id, pod_id)
        self.registry.peers[pod_id] = peer
        try:
            selected = await asyncio.to_thread(
                self.allocator.selected, scope, start_id, pod_id
            )
            await socket.send_json(
                {
                    "type": "activate" if selected else "drain",
                    "pod_id": pod_id,
                    "id": "selection",
                }
            )
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    break
                data = object_dict(json.loads(message.data))
                peer.seen = time.monotonic()
                certificate = data.get("certificate")
                if isinstance(certificate, str) and len(certificate) <= 8192:
                    peer.certificate = certificate
                if data.get("type") in ("ready", "heartbeat"):
                    selected = (await asyncio.to_thread(self.fence) and await asyncio.to_thread(
                        self.allocator.selected, scope, start_id, pod_id
                    ))
                    peer.ready = (
                        selected
                        and data.get("ready") is True
                        and data.get("proof_key_sha256") == self.proof_hash
                        and data.get("pricing_sha256") == self.pricing_hash
                    )
                    if not selected:
                        await socket.send_json({"type": "drain", "id": "selection"})
                elif data.get("type") == "reply":
                    pending = peer.replies.get(str(data.get("id")))
                    if pending is not None and not pending.done():
                        pending.set_result(data)
                elif data.get("type") == "drained":
                    peer.ready = False
                    peer.drained = True
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

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=16384)
        app.add_routes([
            web.get("/health", self.health),
            web.get("/pods/current", self.current),
            web.post("/pods/start", self.start),
            web.get("/pods/{start_id}", self.pod_status),
            web.get("/control/{hotkey}/{start_id}", self.pod_control),
        ])
        return app
