"""Real HTTPS/WebSocket gateway, in-memory R2, simulated provider capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

import aiohttp
import httpx
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import make_mocked_request
from bittensor_wallet import Keypair
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from support import MemoryState, Provider
from test_recording import PRICING, legacy_retry
from witness_client import ReplayMismatch, ReplayWitness, WitnessClient, WitnessError
from witness_client.auth import hotkey_headers
from witness_client.calls import Invocation, RequestKey, canonical
from witness_client.gateway import GatewayClient
from witness_client.pods import PodGatewayClient, Snapshot
from witness_client.request import serialize_body
from witness_client.tunnel import WebSocketTunnel

from pod_gateway.auth import Authenticator, Membership
from pod_gateway.budget import Budget
from pod_gateway.control import DrainControl, Registry
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope, object_dict, string
from pod_gateway.records import Call, CallRecords
from pod_gateway.replay import export_bundle
from pod_gateway.security import capture_token
from pod_gateway.server import Gateway

if TYPE_CHECKING:
    from witness_client._tls import TLSAbruptCloseError
else:
    from tlslite.errors import TLSAbruptCloseError

SECRET = b"s" * 32
PROOF = "c" * 64


@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.filterwarnings("ignore:It is recommended to use web.RequestKey instances")
def test_fresh_signature_does_not_expire_while_waiting_for_runpod(
    monkeypatch: pytest.MonkeyPatch, selected: bool
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    headers = hotkey_headers(key, b"", method="GET", path="/pods/current")
    clock = [int(headers["x-bittensor-nonce"])]
    monkeypatch.setattr("pod_gateway.auth.time.time_ns", lambda: clock[0])
    membership = Membership("test", 17, "test-1", key.ss58_address)
    now = int(time.time() * 1000)
    membership.snapshot = {
        "version": 1, "network": "finney", "netuid": 17, "block": 1,
        "fetched_at": now, "block_time": now, "hotkeys": [],
    }
    handled: list[str] = []

    def fence() -> bool:
        clock[0] += 11_000_000_000
        return selected

    async def handler(request: web.Request) -> web.Response:
        handled.append(request["hotkey"])
        return web.Response()

    async def scenario() -> None:
        request = make_mocked_request(
            "GET", "/pods/current", headers=headers, client_max_size=0,
        )
        gateway = cast(Gateway, SimpleNamespace(auth=Authenticator(membership), fence=fence))
        response = await Gateway.middleware(gateway, request, handler)
        assert response.status == (200 if selected else 503), cast(web.Response, response).text

    asyncio.run(scenario())
    assert handled == ([key.ss58_address] if selected else [])


def tls(directory: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    client = ssl.create_default_context(cafile=str(directory / "cert.pem"))
    return server, client


class Fleet:
    def __init__(
        self,
        state: MemoryState,
        records: CallRecords,
        scope: Scope,
        client: aiohttp.ClientSession,
    ) -> None:
        self.state, self.records, self.scope, self.client = (
            state,
            records,
            scope,
            client,
        )
        self.budget = Budget(state, PRICING)
        self.origin = ""
        self.tasks: list[asyncio.Task[None]] = []
        self.jobs: set[str] = set()
        self.paid = 0
        self.failures_remaining = 0
        self.failure_after_dispatch = False
        self.authorization_unconfirmed = False
        self.lose_ack = False

    async def control(self, pod: Pod, body: dict[str, object]) -> None:
        env = object_dict(body["env"])
        token = string(env["WITNESS_POD_CAPABILITY"])
        start_id = pod.markers["WITNESS_START_ID"]
        socket: aiohttp.ClientWebSocketResponse | None = None
        for _ in range(100):
            try:
                socket = await self.client.ws_connect(
                    f"{self.origin}/control/{self.scope.hotkey}/{start_id}",
                    headers={
                        "Authorization": "Bearer " + token,
                        "x-witness-pod": pod.id,
                    },
                )
                break
            except aiohttp.WSServerHandshakeError:
                await asyncio.sleep(0.01)
        assert socket is not None
        async with socket:
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    break
                value = object_dict(json.loads(message.data))
                if value["type"] == "activate":
                    await socket.send_json(
                        {
                            "type": "ready",
                            "ready": True,
                            "proof_key_sha256": PROOF,
                            "pricing_sha256": self.budget.pricing_hash,
                        }
                    )
                elif value["type"] == "capture":
                    invocation, call = (
                        Invocation.model_validate(value["invocation"]),
                        Call.model_validate(value["call"]),
                    )
                    if call.exchange_id not in self.jobs:
                        self.jobs.add(call.exchange_id)
                        self.tasks.append(
                            asyncio.create_task(self.capture(invocation, call, token))
                        )
                    await socket.send_json(
                        {"type": "reply", "id": value["id"], "accepted": True}
                    )

    async def capture(self, invocation: Invocation, call: Call, token: str) -> None:
        assert call.capture_id is not None and call.pod_id is not None
        prefix = f"rounds/{self.scope.round}/{self.scope.hotkey}/{call.exchange_id}/"
        session = {
            "id": call.exchange_id,
            "protocol": 2,
            "provider": (
                "https://chatgpt.com/backend-api/codex/responses"
                if invocation.request.provider == "codex" else "https://api.openai.com/v1/responses"
            ),
            "proof_key_sha256": PROOF,
            "round": self.scope.round,
            "miner": self.scope.hotkey,
        }
        async with self.client.ws_connect(
            f"{self.origin}/worker/exchanges/{call.exchange_id}",
            headers={
                "Authorization": "Bearer "
                + capture_token(token, call.exchange_id, call.capture_id),
                "x-witness-hotkey": self.scope.hotkey,
            },
        ) as socket:
            self.state.put(prefix + "session.json", canonical(session))
            await socket.send_json({"type": "ready", "session": session})
            value = object_dict(await socket.receive_json())
            body = string(value["fixture_body"]).encode()
            if self.failures_remaining and not self.failure_after_dispatch and not self.lose_ack:
                self.failures_remaining -= 1
                self.records.failed(
                    self.scope, invocation, call.pod_id, "TimeoutError", exchange_id=call.exchange_id,
                    not_dispatched=True,
                )
                await socket.send_json({"type": "end", "fixture_failure": True})
                await socket.send_json({"type": "finished"})
                return
            async with self.client.get(
                f"{self.origin}/worker/exchanges/{call.exchange_id}/selection",
                headers={
                    "Authorization": "Bearer "
                    + capture_token(token, call.exchange_id, call.capture_id),
                    "x-witness-hotkey": self.scope.hotkey,
                },
            ) as check:
                assert check.status == 200 and (await check.json())["selected"] is True
            self.budget.reserve(self.scope, invocation, call.exchange_id, body)
            if not self.failures_remaining or not self.authorization_unconfirmed:
                self.records.authorize(self.scope, invocation, call.pod_id, exchange_id=call.exchange_id)
                self.paid += 1
            if self.failures_remaining:
                self.failures_remaining -= 1
                if self.failure_after_dispatch:
                    self.budget.settle(self.scope, invocation, None, exchange_id=call.exchange_id)
                    self.records.failed(self.scope, invocation, call.pod_id, "TimeoutError", exchange_id=call.exchange_id)
                await socket.send_json({"type": "end", "fixture_failure": True})
                if self.failure_after_dispatch:
                    await socket.send_json({"type": "finished"})
                    return
            answer = f"answer-{self.paid}"
            await asyncio.sleep(0.05)
            usage = {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10}
            response = (
                b"data: "
                + canonical(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": answer}
                                    ],
                                }
                            ],
                            "usage": usage,
                        },
                    }
                )
                + b"\n\n"
            )
            metadata = {
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "response_sha256": hashlib.sha256(response).hexdigest(),
                "provider_status": 200,
                "usage": usage,
                "generation_id": invocation.generation.id,
                "generation": invocation.generation.model_dump(),
                "request_key": invocation.request.model_dump(),
                "call_id": invocation.request.id,
                "hotkey": self.scope.hotkey,
                "round": self.scope.round,
                "exchange_id": call.exchange_id,
                "pod_id": call.pod_id,
                "pod_start_id": call.start_id,
                "proof_key_sha256": PROOF,
                "pricing_sha256": self.budget.pricing_hash,
            }
            for name, data in {
                "request.json": body,
                "response.sse": response,
                "manifest.json": canonical(metadata),
                "client.bin": b"fixture",
                "server.bin": b"fixture",
                "authorization-server.bin": b"fixture",
                "claim.json": b"{}",
            }.items():
                self.state.put(prefix + name, data)
            self.state.put(
                prefix + "status.json",
                canonical(
                    {
                        "status": "issued",
                        "provider_status": 200,
                        "evidence_expires": int((time.time() + 86400) * 1000),
                    }
                ),
            )
            self.budget.settle(self.scope, invocation, usage, exchange_id=call.exchange_id)
            self.records.issued(
                self.scope, invocation, call.pod_id, canonical(metadata), exchange_id=call.exchange_id
            )
            with suppress(aiohttp.ClientConnectionError):
                await socket.send_json({"type": "end"})
                await socket.send_json({"type": "finished"})


@pytest.mark.parametrize("call_limit", [1, 2])
@pytest.mark.parametrize("failure", [
    "none", "before_dispatch", "after_dispatch", "authorization_unconfirmed", "lost_ack", "legacy", "legacy_unconfirmed",
])
@pytest.mark.parametrize("subscription", [False, True])
def test_https_gateway_deduplicates_inflight_requests_and_replays_without_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call_limit: int, failure: str, subscription: bool
) -> None:
    fail_first = failure == "before_dispatch"

    async def scenario() -> None:
        server_tls, client_tls = tls(tmp_path)
        monkeypatch.setattr(
            "witness_client.client.ssl.create_default_context", lambda **_: client_tls
        )

        class Prover:
            verifying_key_sha256 = PROOF

            def __init__(self, _: Path) -> None:
                pass

        monkeypatch.setattr("witness_client.client.Prover", Prover)
        monkeypatch.setattr(
            "witness_client.request.subscription_credentials", lambda *args: ("fixture-" + "a" * 80, "abc123")
        )
        sent_sessions: list[bytes | None] = []

        def exchange(
            tunnel: WebSocketTunnel,
            host: str,
            chunks: tuple[tuple[bool, bytes], ...],
            **kwargs: object,
        ) -> bytes:
            headers = b"".join(chunk for private, chunk in chunks if not private).partition(b"\r\n\r\n")[0]
            sent_sessions.append(next(
                (line for line in headers.split(b"\r\n") if line.startswith(b"session-id:")), None
            ))
            body = chunks[-1][1].partition(b"\r\n\r\n")[2]
            tunnel.socket.send(json.dumps({"fixture_body": body.decode()}))
            while True:
                message = tunnel.socket.recv(timeout=10)
                if (
                    isinstance(message, str)
                    and json.loads(message).get("type") == "end"
                ):
                    if json.loads(message).get("fixture_failure") is True:
                        raise TLSAbruptCloseError()
                    return b""

        monkeypatch.setattr("witness_client.client.exchange", exchange)
        key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
        scope = Scope("competition", "test-0", key.ss58_address)
        state, provider, registry = MemoryState(), Provider(), Registry()
        records = CallRecords(state)
        membership = Membership(scope.deployment, 17, scope.round)
        now = int(time.time() * 1000)
        membership.snapshot = {
            "version": 1,
            "network": "finney",
            "netuid": 17,
            "block": 1,
            "block_time": now,
            "fetched_at": now,
            "hotkeys": [scope.hotkey],
        }
        spec = PodSpec("registry/witness@sha256:" + "a" * 64, capability_secret=SECRET)
        allocator = Allocator(provider, DrainControl(registry), spec)
        image = b"frozen PNG"
        gateway = Gateway(
            allocator=allocator,
            records=records,
            budget=Budget(state, PRICING),
            auth=Authenticator(membership),
            registry=registry,
            evidence=state,
            deployment=scope.deployment,
            round_number=scope.round,
            round_end=int(time.time()) + 600,
            secret=SECRET,
            proof_hash=PROOF,
            prompts=frozenset({hashlib.sha256(image).hexdigest()}),
            fence=lambda: True,
            pricing_hash=hashlib.sha256(PRICING).hexdigest(),
        )
        runner = web.AppRunner(gateway.application())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
        await site.start()
        server = cast(asyncio.Server, site._server)
        assert server.sockets
        origin = f"https://localhost:{server.sockets[0].getsockname()[1]}"
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=client_tls)
        ) as http:
            fleet = Fleet(state, records, scope, http)
            fleet.failures_remaining = int(failure not in ("none", "legacy", "legacy_unconfirmed"))
            fleet.failure_after_dispatch = failure in (
                "after_dispatch", "authorization_unconfirmed", "legacy", "legacy_unconfirmed",
            )
            fleet.authorization_unconfirmed = failure == "authorization_unconfirmed"
            fleet.lose_ack = failure == "lost_ack"
            fleet.origin = origin
            loop = asyncio.get_running_loop()

            def created(pod: Pod, body: dict[str, object]) -> None:
                async def attach() -> None:
                    for _ in range(200):
                        actual = provider.get(pod.id)
                        if actual is not None and actual.lifecycle == "active":
                            await fleet.control(actual, body)
                            return
                        await asyncio.sleep(0.01)

                loop.call_soon_threadsafe(
                    lambda: fleet.tasks.append(asyncio.create_task(attach()))
                )

            provider.on_create = created
            legacy_exchange_id: str | None = None

            def run() -> None:
                nonlocal legacy_exchange_id
                with WitnessClient(
                    {
                        "witness_url": origin,
                        "proof_parameters": str(tmp_path),
                        "state_directory": str(tmp_path / "client"),
                        "transport": "pods-v1",
                        "round": "test-0",
                    },
                    hotkey=key,
                    openai_api_key=None if subscription else "fixture-" + "a" * 80,
                    subscription="codex" if subscription else None,
                    delivery_timeout=20,
                ) as witness:
                    if fleet.failure_after_dispatch:
                        assert isinstance(witness._gateway, PodGatewayClient)
                        witness._gateway.ensure_pod()
                    bound = witness.for_generation(
                        image, 42, call_slots=threading.BoundedSemaphore(call_limit)
                    )
                    original = replacement = None
                    if failure in ("legacy", "legacy_unconfirmed"):
                        mode: Literal["api", "codex"] = "codex" if subscription else "api"
                        body = serialize_body({
                            "service_tier": "default", "stream": True, "model": "fixture", "input": "same image",
                        }, mode)
                        original, replacement = legacy_retry(
                            state, scope, Invocation(
                                generation=bound.generation, ordinal=0, request=RequestKey.from_body(mode, body, 0),
                            ), body, authorized=failure == "legacy",
                        )
                        legacy_exchange_id = original.exchange_id
                        fleet.paid = int(failure == "legacy")
                    if fleet.failure_after_dispatch:
                        assert isinstance(witness._gateway, PodGatewayClient)
                        reserve = witness._gateway.reserve

                        def short_wait(invocation: Invocation) -> Snapshot:
                            snapshot = reserve(invocation)
                            snapshot.call.deadline = int(time.time()) + 2
                            return snapshot

                        monkeypatch.setattr(witness._gateway, "reserve", short_wait)
                        for attempt in (bound, witness.for_generation(image, 42)):
                            with pytest.raises(WitnessError) as failed:
                                attempt.create(stream=True, model="fixture", input="same image")
                            assert failed.value.code == "capture_in_progress"
                            assert failed.value.source == "gateway"
                            recording = records.load(scope, bound.generation.id)
                            assert recording is not None and len(recording.calls) == 1
                            call = original if original is not None else next(iter(recording.calls.values()))
                            assert failed.value.exchange_id == call.exchange_id
                            request = Invocation(generation=bound.generation, ordinal=0, request=call.request)
                            retry_body = canonical({
                                "invocation": request.model_dump(),
                                "exchange_id": call.exchange_id if replacement is None else replacement.exchange_id,
                            })
                            response = witness._http.post(
                                "/calls/retry", content=retry_body,
                                headers=hotkey_headers(key, retry_body, method="POST", path="/calls/retry"),
                            )
                            assert response.status_code == 200, response.text
                            assert response.json()["call"]["exchange_id"] == call.exchange_id
                            assert response.json()["call"]["phase"] == "failed"
                            asserted_body = canonical({
                                "invocation": request.model_dump(), "exchange_id": call.exchange_id,
                                "not_dispatched": True,
                            })
                            asserted = witness._http.post(
                                "/calls/retry", content=asserted_body,
                                headers=hotkey_headers(key, asserted_body, method="POST", path="/calls/retry"),
                            )
                            assert asserted.status_code == 400
                            if original is not None:
                                assert recording.attempts == {original.exchange_id: original}
                                assert replacement is not None
                                assert recording.calls[request.request.id] == replacement
                                dispatch_body = canonical({
                                    "invocation": request.model_dump(), "start_id": witness._gateway.ensure_pod(),
                                })
                                dispatch = witness._http.post(
                                    "/calls/dispatch", content=dispatch_body,
                                    headers=hotkey_headers(key, dispatch_body, method="POST", path="/calls/dispatch"),
                                )
                                assert dispatch.status_code == 202, dispatch.text
                                assert dispatch.json()["call"]["exchange_id"] == original.exchange_id
                                assert not fleet.jobs
                            else:
                                assert recording.attempts == {} and len(fleet.jobs) == 1
                            assert fleet.paid == int(failure in ("after_dispatch", "legacy"))
                            summary = gateway.budget.summary(scope, request)
                            assert summary["possible_dispatches"] == summary["pending_usage"] == 1
                            assert summary["remaining_credits"] is None
                            with pytest.raises(WitnessError) as unfinished:
                                attempt.finish(None)
                            assert unfinished.value.code == "recording_unresolved"
                        return
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [
                            pool.submit(
                                bound.create,
                                model="fixture",
                                input="same image",
                                session_id=f"fixture-cache-{index}" if subscription else None,
                            )
                            for index in range(2)
                        ]
                        answers = [
                            future.result(timeout=30).output_text for future in futures
                        ]
                    assert answers == ["answer-1", "answer-1"] and fleet.paid == 1
                    if fail_first:
                        assert len(sent_sessions) == 2 and sent_sessions[0] == sent_sessions[1]
                    cache_session = sent_sessions[0].decode().removeprefix("session-id: ") if sent_sessions[0] else None
                    fresh = bound.create(
                        stream=True, model="fixture", input="same image", request_id=1,
                        session_id=cache_session,
                    )
                    assert fresh.output_text == "answer-2" and fleet.paid == 2
                    assert len(sent_sessions) == 2 + int(fail_first)
                    assert sent_sessions[-1] == sent_sessions[0]
                    assert (sent_sessions[0] is not None) == subscription
                    assert witness.resume(fresh._exchange_id).output_text == "answer-2"
                    bound.finish("final source")

                    resumed = witness.for_generation(image, 42)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [
                            pool.submit(
                                resumed.create, stream=True, model="fixture", input="same image",
                                session_id="resumed-cache" if subscription else None,
                            )
                            for _ in range(2)
                        ]
                        assert [future.result(timeout=10).output_text for future in futures] == answers
                    assert resumed.create(
                        stream=True, model="fixture", input="same image", request_id=1,
                        session_id=None,
                    ).output_text == "answer-2"
                    resumed.finish("final source")
                    assert fleet.paid == 2 and len(sent_sessions) == 2 + int(fail_first)

                    def verified(_: str, directory: Path) -> dict[str, object]:
                        return object_dict(
                            json.loads((directory / "manifest.json").read_bytes())
                        )

                    output = tmp_path / "verified"
                    export_bundle(
                        records=records,
                        evidence=state,
                        scope=scope,
                        generation_id=bound.generation.id,
                        output=output,
                        proof_hash=PROOF,
                        pricing_hash=hashlib.sha256(PRICING).hexdigest(),
                        verify=verified,
                    )
                    replay = ReplayWitness(output, timeout=5)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [
                            pool.submit(
                                replay.create,
                                model="fixture",
                                input="same image",
                                session_id="replay-cache" if subscription else None,
                            )
                            for _ in range(2)
                        ]
                        assert [
                            f.result(timeout=10).output_text for f in futures
                        ] == answers
                    assert (
                        replay.create(
                            stream=True,
                            model="fixture",
                            input="same image",
                            request_id=1,
                        ).output_text
                        == "answer-2"
                    )
                    replay.finish("final source")
                    with pytest.raises(ReplayMismatch):
                        replay.create(stream=True, model="fixture", input="extra")
                    assert fleet.paid == 2 and provider.creates == 1
                    old = allocator.current(scope)
                    assert old is not None and old.pod_id is not None
                    provider.delete(old.pod_id)
                    assert isinstance(witness._gateway, PodGatewayClient)
                    renewed = witness._gateway.ensure_pod()
                    assert renewed != old.start_id and provider.creates == 2
                    assert fleet.paid == 2

            try:
                await asyncio.wait_for(asyncio.to_thread(run), 60)
                # Another registered identity cannot read this miner's exchange.
                other = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
                membership_value = dict(membership.snapshot or {})
                membership_value["hotkeys"] = [scope.hotkey, other.ss58_address]
                membership.snapshot = membership_value
                exchange_id = next(iter(fleet.jobs), legacy_exchange_id)
                assert exchange_id is not None
                path = f"/exchanges/{exchange_id}/response.sse"
                async with http.get(
                    origin + path,
                    headers=hotkey_headers(other, b"", method="GET", path=path),
                ) as response:
                    assert response.status == 404
            finally:
                try:
                    await registry.close()
                    for task in fleet.tasks:
                        task.cancel()
                    for task in fleet.tasks:
                        with suppress(asyncio.CancelledError):
                            await task
                finally:
                    await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["miner", "worker_capture", "worker_control"])
def test_gateway_upgrade_during_tls_session_ticket_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    from websockets.sync.client import connect

    async def scenario() -> None:
        server_tls, client_tls = tls(tmp_path)
        reading_ticket, sent_upgrade = threading.Event(), threading.Event()
        original_send = ssl.SSLSocket.send

        def ticket(
            socket: ssl.SSLSocket | ssl.SSLObject,
            direction: str,
            version: int,
            content_type: int,
            message_type: int,
            data: bytes,
        ) -> None:
            # TLS 1.3 NewSessionTicket, while the reader is inside SSL_read.
            if direction == "read" and content_type == 22 and message_type == 4:
                reading_ticket.set()
                assert sent_upgrade.wait(2)

        def send(socket: ssl.SSLSocket, data: bytes, flags: int = 0) -> int:
            if not bytes(data).startswith(b"GET "):
                return original_send(socket, data, flags)
            # An eager reader enters ticket processing before this write.
            reading_ticket.wait(0.1)
            try:
                return original_send(socket, data, flags)
            finally:
                sent_upgrade.set()

        monkeypatch.setattr(client_tls, "_msg_callback", ticket)
        monkeypatch.setattr(ssl.SSLSocket, "send", send)
        monkeypatch.setattr(ssl, "create_default_context", lambda **_: client_tls)
        monkeypatch.setattr(
            "witness_client.gateway.connect",
            lambda uri, **options: connect(uri, **{**options, "open_timeout": 1}),
        )
        monkeypatch.setattr(
            "pod.connect",
            lambda uri, **options: connect(uri, **{**options, "open_timeout": 1}),
        )
        received: list[str] = []

        async def echo(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            if role == "worker_control":
                await socket.send_json({"type": "drain", "id": "fixture"})
                await socket.receive_str()  # Drain acknowledgement.
                received.append(await socket.receive_str())
            else:
                value = await socket.receive_str()
                received.append(value)
                if role == "worker_capture":
                    await socket.send_bytes(b"upgrade arrived")
                else:
                    await socket.send_str(value)
            await socket.close()
            return socket

        app = web.Application()
        app.router.add_get("/exchanges/{exchange_id}/connect", echo)
        app.router.add_get("/worker/exchanges/{exchange_id}", echo)
        app.router.add_get("/control/{hotkey}/{start_id}", echo)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
        await site.start()
        server = cast(asyncio.Server, site._server)
        assert server.sockets
        origin = f"https://localhost:{server.sockets[0].getsockname()[1]}"
        key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())

        def run() -> None:
            if role == "miner":
                with httpx.Client(base_url=origin) as http:
                    gateway = GatewayClient(http, key, tmp_path, 1)
                    with gateway.tunnel("fixture") as tunnel:
                        tunnel.socket.send("upgrade arrived")
                        assert tunnel.socket.recv(timeout=1) == "upgrade arrived"
                return

            from channel import WorkerChannel
            from pod import PodRuntime
            from test_recording import SCOPE
            from test_recovery import START, capture

            state = MemoryState()
            records, budget, item, call = capture(state)
            worker = PodRuntime(
                evidence=state, records=records, budget=budget, scope=SCOPE,
                start_id=START, pod_id="pod-one", token="ab" * 32, origin=origin,
                proof_hash=PROOF, round_end=int(time.time()) + 3, spool=tmp_path / "spool",
            )

            def relay(*args: object, **kwargs: object) -> None:
                channel = cast(WorkerChannel, args[5])
                assert channel.recv(time.monotonic() + 1) == b"upgrade arrived"
                raise RuntimeError("No provider is used in this test")

            monkeypatch.setattr("pod.relay", relay)
            try:
                if role == "worker_capture":
                    worker._capture(item, call)
                    located = records.locate(SCOPE, call.exchange_id)
                    assert located is not None and located[1].phase == "failed"
                    assert located[1].failure == "RuntimeError"
                else:
                    worker.run()
            finally:
                worker.executor.shutdown(wait=True)

        try:
            await asyncio.wait_for(asyncio.to_thread(run), 5)
            assert reading_ticket.is_set()
            if role == "miner":
                assert received == ["upgrade arrived"]
            elif role == "worker_capture":
                assert len(received) == 1 and json.loads(received[0])["type"] == "ready"
            else:
                assert received == ['{"type":"drained"}']
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
