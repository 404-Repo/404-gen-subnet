"""Local test entry point; never included in the deployed bundle."""

from __future__ import annotations

import uuid
from typing import cast

from workers import Request

from bindings import DurableState, HttpResponse
from models import Connection, Env
from runtime import js_object, json
from worker import Default as Default, MinerAdmission as Admission


class MinerAdmission(Admission):
    def __init__(self, ctx: DurableState, env: Env) -> None:
        super().__init__(ctx, env)
        self.instance = uuid.uuid4().hex

    async def fetch(self, request: Request) -> HttpResponse:
        if request.url != "https://internal/test-state":
            return await super().fetch(request)
        connections: list[Connection] = []
        for role in ("miner", "worker"):
            for socket in self.ctx.getWebSockets(
                f"{request.headers['x-test-id']}/{role}"
            ):
                attachment = cast(Connection, socket.deserializeAttachment().to_py())
                if request.method == "POST" and role == "miner":
                    attachment["messages"] = 4095
                    socket.serializeAttachment(js_object(attachment))
                connections.append(attachment)
        return json({"instance": self.instance, "connections": connections})
