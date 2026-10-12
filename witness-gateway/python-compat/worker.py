"""Isolated runtime probe for bundled sr25519 and hibernatable binary relay."""

from __future__ import annotations

import hashlib
import re
import time
import uuid

from js import Object, Response as JSResponse, WebSocketPair
from pyodide.ffi import JsProxy, to_js
from workers import DurableObject, Request, Response, WorkerEntrypoint, import_from_javascript


class Default(WorkerEntrypoint):
    async def fetch(self, request: Request) -> Response | JsProxy:
        path = request.url.split("/", 3)[3]
        hotkey = request.headers.get("x-bittensor-hotkey") or ""
        nonce = request.headers.get("x-bittensor-nonce") or ""
        signature = request.headers.get("x-bittensor-signature") or ""
        if not re.fullmatch(r"[1-9][0-9]{15,19}", nonce):
            return Response("invalid nonce", status=401)
        age = time.time_ns() - int(nonce)
        if age > 10_000_000_000 or age < -2_000_000_000:
            return Response("stale nonce", status=401)
        if not re.fullmatch(r"0x[0-9a-f]{128}", signature):
            return Response("invalid signature", status=401)
        message = "\n".join(("btauth/1", "sr25519", request.method, "/" + path,
                              hashlib.sha256(b"").hexdigest(), nonce, hotkey, "404-witness"))
        crypto = import_from_javascript("dist/crypto.mjs")
        if not await crypto.verify(message, signature[2:], hotkey):
            return Response("invalid signature", status=401)
        if path == "verify":
            return Response("verified")
        return await self.env.RELAY.getByName(hotkey).fetch(request.js_object)


class Relay(DurableObject):
    def __init__(self, ctx: JsProxy, env: JsProxy) -> None:
        super().__init__(ctx, env)
        self.instance = str(uuid.uuid4())

    async def fetch(self, request: Request) -> Response | JsProxy:
        role = request.url.rsplit("/", 1)[-1]
        if role == "state":
            return Response.from_json({"instance": self.instance, "attachments": [
                socket.deserializeAttachment().to_py() for socket in self.ctx.getWebSockets()
            ]})
        if role not in ("miner", "worker") or request.headers.get("Upgrade") != "websocket":
            return Response("unknown route", status=404)
        client, server = WebSocketPair.new().object_values()
        self.ctx.acceptWebSocket(server, to_js([role]))
        server.serializeAttachment(to_js({"role": role, "messages": 0}, dict_converter=Object.fromEntries))
        return JSResponse.new(None, to_js({"status": 101, "webSocket": client}, dict_converter=Object.fromEntries))

    async def webSocketMessage(self, socket: JsProxy, message: str | JsProxy) -> None:
        attachment = socket.deserializeAttachment()
        attachment.messages += 1
        socket.serializeAttachment(attachment)
        role = "worker" if attachment.role == "miner" else "miner"
        for peer in self.ctx.getWebSockets(role):
            peer.send(message)

    async def webSocketClose(self, socket: JsProxy, code: int, reason: str, clean: bool) -> None:
        socket.close(1000, "Closed")
