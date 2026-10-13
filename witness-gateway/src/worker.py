"""Public admission and private allocation for queue-based witness workers."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from functools import partial
from typing import cast
from urllib.parse import quote

from js import AbortSignal, Request as JSRequest, Response, URL, WebSocketPair, fetch
from pyodide.ffi import JsProxy
from workers import DurableObject, Request, WorkerEntrypoint

from auth import UUID, authenticate, exchange_payload
from bindings import Buffer, DurableState, HttpResponse, Socket
from membership import refresh_membership
from models import (
    Connection,
    Env,
    GatewayRequest,
    Job,
    Json,
    Rate,
    Rejection,
    Role,
    WorkerConnection,
)
from runtime import (
    bounded_text,
    dumps,
    failure,
    js_object,
    json,
    loads,
    now_ms,
    safe_integer,
    stored,
    with_state_lock,
)


def require_open_round(env: Env) -> int:
    try:
        end = float(env.ROUND_END or "0")
    except ValueError:
        raise Rejection(403, "round_closed") from None
    if not safe_integer(end) or now_ms() / 1000 >= end:
        raise Rejection(403, "round_closed")
    return int(end)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Default(WorkerEntrypoint):
    env: Env

    async def scheduled(self, controller: JsProxy, env: Env, ctx: JsProxy) -> None:
        try:
            await refresh_membership(self.env)
        except Exception as error:
            print(
                dumps(
                    {
                        "event": "membership_refresh_failed",
                        "network": self.env.NETWORK,
                        "netuid": self.env.NETUID,
                        "error": str(error),
                    }
                )
            )
            # Leave the saved snapshot untouched and retry on the next cron tick.

    async def fetch(self, request: Request) -> HttpResponse:
        try:
            require_open_round(self.env)
            native = request.js_object
            url = URL.new(request.url)
            worker = re.fullmatch(
                r"/workers/([a-z0-9][a-z0-9-]{0,63})/([1-9A-HJ-NP-Za-km-z]{48})/([0-9a-f-]{36})",
                url.pathname,
            )
            if worker:
                if (
                    request.method != "GET"
                    or url.search
                    or worker[1] != self.env.ROUND
                    or not UUID.fullmatch(worker[3])
                    or (native.headers.get("Upgrade") or "").lower() != "websocket"
                ):
                    raise Rejection(404, "unknown_route")
                token = (native.headers.get("Authorization") or "").removeprefix(
                    "Bearer "
                )
                if not re.fullmatch(r"[0-9a-f]{64}", token):
                    raise Rejection(401, "invalid_worker_token")
                stub = self.env.MINERS.getByName(f"{self.env.ROUND}/{worker[2]}")
                return await stub.fetch(
                    JSRequest.new(
                        "https://internal/worker",
                        js_object(
                            {
                                "headers": {
                                    "Upgrade": "websocket",
                                    "x-witness-input": dumps(
                                        {"requestId": worker[3], "token": token}
                                    ),
                                },
                            }
                        ),
                    )
                )
            input = await authenticate(native, self.env)
            if (
                input.get("connect")
                and (native.headers.get("Upgrade") or "").lower() != "websocket"
            ):
                raise Rejection(426, "websocket_required")
            stub = self.env.MINERS.getByName(f"{self.env.ROUND}/{input['hotkey']}")
            options = (
                {"headers": {"Upgrade": "websocket", "x-witness-input": dumps(input)}}
                if input.get("connect")
                else {"method": "POST", "body": dumps(input)}
            )
            grant = await stub.fetch(
                JSRequest.new("https://internal/miner", js_object(options))
            )
            if not input.get("object") or not grant.ok:
                return grant
            if grant.body:
                await grant.body.cancel()
            prefix = f"rounds/{self.env.ROUND}/{input['hotkey']}/{input['requestId']}"
            if input["object"] == "status.json":
                completed = await self.completed_status(input, prefix)
                if completed is not None:
                    return completed
            key = f"{prefix}/{input['object']}"
            evidence = await self.env.TRANSCRIPTS.get(key)
            if not evidence:
                raise Rejection(404, "object_not_ready")
            if evidence.size > 16 * 1024 * 1024:
                await evidence.body.cancel()
                raise Rejection(413, "object_too_large")
            return Response.new(
                evidence.body,
                js_object(
                    {
                        "headers": {
                            "content-type": (
                                "application/json"
                                if input["object"] == "status.json"
                                else "application/octet-stream"
                            ),
                            "cache-control": "no-store",
                            "content-length": str(evidence.size),
                        }
                    }
                ),
            )
        except Exception as error:
            return failure(error)

    async def completed_status(
        self, input: GatewayRequest, prefix: str
    ) -> HttpResponse | None:
        # The worker writes the manifest after verification and all evidence uploads.
        # It remains authoritative if the subsequent status write fails.
        manifest = await self.env.TRANSCRIPTS.get(prefix + "/manifest.json")
        if not manifest:
            return None
        data = loads(await bounded_text(manifest.body, 64 * 1024))
        if not isinstance(data, dict):
            raise Rejection(503, "invalid_evidence_manifest")
        verified_at, provider_status = data.get("verified_at"), data.get(
            "provider_status"
        )
        if (
            data.get("prefix") != prefix
            or data.get("round") != self.env.ROUND
            or data.get("hotkey") != input["hotkey"]
            or data.get("exchange_id") != input["requestId"]
            or isinstance(verified_at, bool)
            or not isinstance(verified_at, (int, float))
            or not math.isfinite(verified_at)
            or verified_at <= 0
            or isinstance(provider_status, bool)
            or not isinstance(provider_status, int)
            or not 100 <= provider_status <= 599
            or not isinstance(data.get("provider_rejected"), bool)
        ):
            raise Rejection(503, "invalid_evidence_manifest")
        usage = data.get("usage")
        return json(
            {
                "status": "issued",
                "authorized": True,
                "usage": usage,
                "provider_status": provider_status,
                "usage_state": (
                    "rejected"
                    if data["provider_rejected"]
                    else "recorded" if usage is not None else "unknown"
                ),
                "evidence_expires": int((verified_at + 7 * 86400) * 1000),
            }
        )


class MinerAdmission(DurableObject):
    """One round/hotkey owns persisted admission state and its two live sockets."""

    env: Env
    ctx: DurableState

    async def job(self, request_id: str) -> Job | None:
        return cast(Job | None, await stored(self.ctx.storage, f"job/{request_id}"))

    async def save_job(self, request_id: str, job: Job) -> None:
        await self.ctx.storage.put(f"job/{request_id}", js_object(job))

    async def fetch(self, request: Request) -> HttpResponse:
        try:
            if URL.new(request.url).pathname == "/worker":
                worker = cast(
                    WorkerConnection,
                    loads(request.js_object.headers.get("x-witness-input") or ""),
                )
                return await with_state_lock(
                    self.ctx, partial(self.attach_worker, worker)
                )
            raw = (
                request.js_object.headers.get("x-witness-input")
                if request.method == "GET"
                else await request.text()
            )
            input = cast(GatewayRequest, loads(raw or ""))
            token = uuid.uuid4().hex + uuid.uuid4().hex
            digest = (
                sha256(exchange_payload(input["exchange"]))
                if "exchange" in input
                else ""
            )
            result = await with_state_lock(
                self.ctx, partial(self.reserve, input, digest, sha256(token))
            )
            if not isinstance(result, dict):
                if not (
                    input.get("exchange")
                    or input.get("object")
                    or input.get("connect")
                    or input.get("release")
                ):
                    return await self.refresh_status(input)
                return result
            await self.dispatch(input, result, token)
            return self.response(input, cast(Job, await self.job(input["requestId"])))
        except Exception as error:
            return failure(error)

    async def refresh_status(self, input: GatewayRequest) -> HttpResponse:
        job = cast(Job, await self.job(input["requestId"]))
        if (
            job.get("runpodId")
            and not job["claimed"]
            and job["state"] in ("submitting", "queued", "uncertain")
        ):
            remote = await self.runpod("/status/" + quote(job["runpodId"], safe=""))
            if remote.get("status") in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            ):
                await with_state_lock(
                    self.ctx, partial(self.retire_unclaimed, input["requestId"])
                )
        return self.response(input, cast(Job, await self.job(input["requestId"])))

    async def retire_unclaimed(self, request_id: str) -> None:
        latest = cast(Job, await self.job(request_id))
        if not latest["claimed"]:
            await self.retire(request_id)

    def response(self, input: GatewayRequest, job: Job) -> HttpResponse:
        return json(
            {
                "round": self.env.ROUND,
                "hotkey": input["hotkey"],
                "exchange_id": input["requestId"],
                "queue_state": job["state"],
                "deadline": job["deadline"],
                "session": job.get("session"),
            },
            202 if job["state"] in ("submitting", "uncertain") else 200,
        )

    async def reserve(
        self, input: GatewayRequest, digest: str, token_hash: str
    ) -> HttpResponse | Job:
        storage, now = self.ctx.storage, now_ms()
        round_end = require_open_round(self.env)
        nonce_key = f"nonce/{input['nonce']}"
        nonces = await storage.list(js_object({"prefix": "nonce/"}))
        for key, expiry in nonces.entries():
            if expiry < now:
                await storage.delete(key)
        if await storage.get(nonce_key):
            raise Rejection(401, "hotkey_auth_replayed")
        rate = cast(Rate, await stored(storage, "rate") or {"at": now, "tokens": 20})
        tokens = min(20, rate["tokens"] + max(0, now - rate["at"]) / 1000 * 2)
        if tokens < 1:
            raise Rejection(429, "request_rate_limited")
        await storage.put(
            js_object(
                {
                    nonce_key: int(input["nonce"]) // 1_000_000 + 10_001,
                    "rate": {"at": now, "tokens": tokens - 1},
                }
            )
        )
        request_id = input["requestId"]
        job = await self.job(request_id)
        if "exchange" not in input:
            if not job:
                raise Rejection(404, "unknown_job")
            if input.get("release"):
                await self.retire(request_id)
                return json({"released": True})
            if input.get("connect"):
                if (
                    job["state"] != "connected"
                    or not job.get("session")
                    or job["minerAttached"]
                    or now / 1000 >= job["deadline"]
                    or await storage.get("current") != request_id
                    or not self.socket(request_id, "worker")
                ):
                    raise Rejection(409, "worker_not_available")
                await self.save_job(request_id, {**job, "minerAttached": True})
                return self.accept(request_id, "miner")
            if input.get("object"):
                return json({"allowed": True})
            return self.response(input, job)
        if job:
            if job["digest"] != digest:
                raise Rejection(409, "exchange_id_conflict")
            return self.response(input, job)
        maximum = float(self.env.MAX_JOBS_PER_MINER)
        if not safe_integer(maximum) or maximum < 1:
            raise ValueError("Invalid job limit")
        count = cast(int, await stored(storage, "count") or 0)
        if count >= maximum:
            raise Rejection(429, "job_limit_reached")
        current = cast(str | None, await stored(storage, "current"))
        exchange = input["exchange"]
        if current and current != exchange.get("replace"):
            raise Rejection(409, "active_request_exists", current)
        if exchange.get("replace") and not await self.job(exchange["replace"]):
            raise Rejection(404, "unknown_replacement")
        if current:
            await self.retire(current)
        reservation: Job = {
            "digest": digest,
            "state": "submitting",
            "deadline": min(round_end, exchange["deadline"]),
            "tokenHash": token_hash,
            "claimed": False,
            "minerAttached": False,
        }
        # Persist the charge and private capability before any external dispatch.
        await storage.put(
            js_object(
                {
                    f"job/{request_id}": reservation,
                    "count": count + 1,
                    "current": request_id,
                }
            )
        )
        await storage.setAlarm(reservation["deadline"] * 1000)
        return reservation

    async def dispatch(
        self, input: GatewayRequest, reservation: Job, token: str
    ) -> None:
        runpod_id: str | None = None
        try:
            lifetime = max(
                1000, min(604_800_000, reservation["deadline"] * 1000 - now_ms())
            )
            remote = await self.runpod(
                "/run",
                {
                    "input": {
                        **input["exchange"],
                        "round": self.env.ROUND,
                        "expires": reservation["deadline"],
                        "gateway": input["origin"],
                        "token": token,
                    },
                    # Queue waiting must not override the endpoint's execution cap.
                    "policy": {"ttl": lifetime},
                },
            )
            remote_id = remote.get("id")
            if not isinstance(remote_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9_-]{1,128}", remote_id
            ):
                raise ValueError("Invalid upstream ID")
            runpod_id = remote_id
        except Exception:
            print(
                dumps(
                    {
                        "event": "runpod_queue_uncertain",
                        "exchange_id": input["requestId"],
                    }
                )
            )
        await with_state_lock(
            self.ctx, partial(self.save_dispatch, input["requestId"], runpod_id)
        )

    async def save_dispatch(self, request_id: str, runpod_id: str | None) -> None:
        job = cast(Job, await self.job(request_id))
        if runpod_id:
            job["runpodId"] = runpod_id
        # A worker claim or replacement may have completed while dispatch waited.
        if job["state"] == "submitting":
            job["state"] = "queued" if runpod_id else "uncertain"
        await self.save_job(request_id, job)

    async def attach_worker(self, input: WorkerConnection) -> HttpResponse:
        require_open_round(self.env)
        request_id = input["requestId"]
        job = await self.job(request_id)
        if not job or job["tokenHash"] != sha256(input["token"]):
            raise Rejection(401, "invalid_worker_token")
        if (
            job["claimed"]
            or job["state"] == "retired"
            or now_ms() / 1000 >= job["deadline"]
            or await self.ctx.storage.get("current") != request_id
        ):
            raise Rejection(409, "worker_not_available")
        await self.save_job(request_id, {**job, "claimed": True, "state": "connected"})
        return self.accept(request_id, "worker")

    def accept(self, request_id: str, role: Role) -> HttpResponse:
        client, server = WebSocketPair.new().object_values()
        self.ctx.acceptWebSocket(server, js_object([f"{request_id}/{role}"]))
        server.serializeAttachment(
            js_object(
                {"requestId": request_id, "role": role, "bytes": 0, "messages": 0}
            )
        )
        # This SDK version drops web_socket; use the native upgrade response.
        return Response.new(None, js_object({"status": 101, "webSocket": client}))

    def socket(self, request_id: str, role: Role) -> Socket | None:
        for socket in self.ctx.getWebSockets(f"{request_id}/{role}"):
            if socket.readyState == 1:
                return socket
        return None

    async def webSocketMessage(self, socket: Socket, message: str | Buffer) -> None:
        connection = cast(Connection, socket.deserializeAttachment().to_py())
        try:
            await with_state_lock(
                self.ctx, partial(self.forward_message, socket, message, connection)
            )
        except Exception:
            await with_state_lock(
                self.ctx, partial(self.retire, connection["requestId"])
            )

    async def forward_message(
        self, socket: Socket, message: str | Buffer, connection: Connection
    ) -> None:
        require_open_round(self.env)
        request_id = connection["requestId"]
        job = await self.job(request_id)
        if (
            not job
            or job["state"] != "connected"
            or now_ms() / 1000 >= job["deadline"]
            or await self.ctx.storage.get("current") != request_id
        ):
            raise ValueError("Retired request")
        length = (
            len(message.encode()) if isinstance(message, str) else message.byteLength
        )
        connection["bytes"] += length
        connection["messages"] += 1
        if (
            length > (2 * 1024 * 1024 if isinstance(message, str) else 256 * 1024)
            or connection["bytes"] > 20 * 1024 * 1024
            or connection["messages"] > 4096
        ):
            raise ValueError("Transport limit")
        socket.serializeAttachment(js_object(connection))
        if isinstance(message, str):
            value = loads(message)
            if not isinstance(value, dict):
                raise ValueError("Invalid message")
            if connection["role"] == "worker" and value.get("type") == "ready":
                session = value.get("session")
                if (
                    job.get("session")
                    or length > 4096
                    or not isinstance(session, dict)
                    or session.get("id") != request_id
                ):
                    raise ValueError("Invalid session")
                await self.save_job(request_id, {**job, "session": session})
                return
            if connection["role"] == "worker" and value.get("type") == "finished":
                await self.save_job(request_id, {**job, "state": "finished"})
                await self.ctx.storage.delete("current")
                return
            allowed = (
                ("handshake", "authorize")
                if connection["role"] == "miner"
                else ("status", "end")
            )
            if value.get("type") not in allowed:
                raise ValueError("Invalid message")
        peer = self.socket(
            request_id, "worker" if connection["role"] == "miner" else "miner"
        )
        if not peer:
            raise ValueError("Peer disconnected")
        peer.send(message)

    async def webSocketClose(
        self, socket: Socket, code: int, reason: str, was_clean: bool
    ) -> None:
        connection = cast(Connection, socket.deserializeAttachment().to_py())
        socket.close(1000, "Exchange closed")
        await with_state_lock(self.ctx, partial(self.retire, connection["requestId"]))

    async def webSocketError(self, socket: Socket, error: JsProxy) -> None:
        await self.webSocketClose(socket, 1000, "Exchange closed", False)

    async def retire(self, request_id: str) -> None:
        job = await self.job(request_id)
        if job and job["state"] != "finished":
            await self.save_job(request_id, {**job, "state": "retired"})
        if await self.ctx.storage.get("current") == request_id:
            await self.ctx.storage.delete("current")
        for role in ("miner", "worker"):
            socket = self.socket(request_id, role)
            if socket:
                socket.close(1000, "Exchange closed")

    async def alarm(self, alarm_info: object = None) -> None:
        await with_state_lock(self.ctx, self.expire_current)

    async def expire_current(self) -> None:
        current = cast(str | None, await stored(self.ctx.storage, "current"))
        if not current:
            return
        job = await self.job(current)
        if job and now_ms() < job["deadline"] * 1000:
            await self.ctx.storage.setAlarm(job["deadline"] * 1000)
        else:
            await self.retire(current)

    async def runpod(self, path: str, body: object = None) -> dict[str, Json]:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]+", self.env.RUNPOD_ENDPOINT)
            or not self.env.RUNPOD_API_KEY
        ):
            raise ValueError("Missing RunPod configuration")
        options: dict[str, object] = {
            "method": "GET" if body is None else "POST",
            "redirect": "manual",
            "signal": AbortSignal.timeout(10_000),
            "headers": {
                "authorization": f"Bearer {self.env.RUNPOD_API_KEY}",
                "content-type": "application/json",
            },
        }
        if body is not None:
            options["body"] = dumps(body)
        response = await fetch(
            f"https://api.runpod.ai/v2/{self.env.RUNPOD_ENDPOINT}{path}",
            js_object(options),
        )
        if not response.ok:
            if response.body:
                await response.body.cancel()
            raise ValueError("RunPod unavailable")
        result = loads(await bounded_text(response.body, 32768))
        if not isinstance(result, dict):
            raise ValueError("Invalid RunPod reply")
        return result
