"""Authenticated outbound Pod control sockets; drain never cancels paid capture."""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
import uuid
from dataclasses import dataclass, field

from aiohttp import web

from .models import Pod, Scope
from .security import capability, sealed
from .state import StateStore


@dataclass
class Peer:
    socket: web.WebSocketResponse
    scope: Scope
    start_id: str
    pod_id: str
    ready: bool = False
    seen: float = field(default_factory=time.monotonic)
    replies: dict[str, asyncio.Future[dict[str, object]]] = field(default_factory=dict)


class Registry:
    def __init__(self) -> None:
        self.peers: dict[str, Peer] = {}
        self.loop = asyncio.get_running_loop()

    def ready(self, pod_id: str) -> bool:
        peer = self.peers.get(pod_id)
        return (
            peer is not None
            and peer.ready
            and not peer.socket.closed
            and time.monotonic() - peer.seen < 45
        )

    async def command(
        self, pod_id: str, command: str, **data: object
    ) -> dict[str, object]:
        peer = self.peers.get(pod_id)
        if peer is None or peer.socket.closed:
            return {}
        request_id = str(uuid.uuid4())
        future = self.loop.create_future()
        peer.replies[request_id] = future
        try:
            await peer.socket.send_json({"type": command, "id": request_id, **data})
            return await asyncio.wait_for(future, 10)
        except (TimeoutError, ConnectionError, RuntimeError):
            return {}
        finally:
            peer.replies.pop(request_id, None)

    async def close(self) -> None:
        for peer in list(self.peers.values()):
            await peer.socket.close()


class DrainControl:
    def __init__(
        self, store: StateStore, secret: bytes, registry: Registry | None = None
    ) -> None:
        self.store, self.secret, self.registry = store, secret, registry

    def drain(self, pod: Pod) -> bool:
        if pod.status == "EXITED":
            # The allocation was already revoked before this check. No process
            # remains to capture/upload; any unresolved call stays unresolved.
            return True
        scope = Scope(
            pod.markers["WITNESS_DEPLOYMENT"],
            pod.markers["WITNESS_ROUND"],
            pod.markers["WITNESS_HOTKEY"],
        )
        start_id = pod.markers["WITNESS_START_ID"]
        token = capability(self.secret, scope, start_id)
        if sealed(self.store, scope, start_id, pod.id, token):
            return True
        if self.registry is None:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.registry.command(pod.id, "drain"), self.registry.loop
        )
        try:
            future.result(timeout=12)
        except (TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
        # Only the durable, permanently fenced seal authorizes deletion.
        return sealed(self.store, scope, start_id, pod.id, token)
