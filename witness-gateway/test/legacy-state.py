"""Seed records using the deployed Python Worker's exact storage conversion."""
import json

from js import Object, Response
from pyodide.ffi import to_js
from workers import DurableObject, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response.new("fixture")


class MinerAdmission(DurableObject):
    async def alarm(self):
        pass

    async def fetch(self, request):
        data = json.loads(await request.text())
        await self.ctx.storage.put(to_js(data["records"], dict_converter=Object.fromEntries))
        await self.ctx.storage.setAlarm(data["deadline"] * 1000)
        return Response.new(self.ctx.id.toString())
