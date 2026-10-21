"""Real HTTPS/WebSocket gateway, in-memory R2, simulated provider capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from bittensor_wallet import Keypair
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from support import MemoryState, Provider
from test_recording import PRICING
from witness_client import ReplayMismatch, ReplayWitness, WitnessClient
from witness_client.auth import hotkey_headers
from witness_client.calls import Invocation, canonical
from witness_client.pods import PodGatewayClient
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

SECRET = b"s" * 32
PROOF = "c" * 64


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
            "provider": "https://api.openai.com/v1/responses",
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
            self.records.authorize(self.scope, invocation, call.pod_id)
            self.paid += 1
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
            self.budget.settle(self.scope, invocation, usage)
            self.records.issued(
                self.scope, invocation, call.pod_id, canonical(metadata)
            )
            await socket.send_json({"type": "end"})
            await socket.send_json({"type": "finished"})


def test_https_gateway_deduplicates_inflight_requests_and_replays_without_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

        def exchange(
            tunnel: WebSocketTunnel,
            host: str,
            chunks: tuple[tuple[bool, bytes], ...],
            **kwargs: object,
        ) -> bytes:
            body = chunks[-1][1].partition(b"\r\n\r\n")[2]
            tunnel.socket.send(json.dumps({"fixture_body": body.decode()}))
            while True:
                message = tunnel.socket.recv(timeout=10)
                if (
                    isinstance(message, str)
                    and json.loads(message).get("type") == "end"
                ):
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

            def run() -> None:
                with WitnessClient(
                    {
                        "witness_url": origin,
                        "proof_parameters": str(tmp_path),
                        "state_directory": str(tmp_path / "client"),
                        "transport": "pods-v1",
                        "round": "test-0",
                    },
                    hotkey=key,
                    openai_api_key="fixture-" + "a" * 80,
                    delivery_timeout=20,
                ) as witness:
                    bound = witness.for_generation(image, 42, "a" * 64)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [
                            pool.submit(
                                bound.create,
                                stream=True,
                                model="fixture",
                                input="same image",
                            )
                            for _ in range(2)
                        ]
                        answers = [
                            future.result(timeout=30).output_text for future in futures
                        ]
                    assert answers == ["answer-1", "answer-1"] and fleet.paid == 1
                    fresh = bound.create(
                        stream=True, model="fixture", input="same image", witness_seed=1
                    )
                    assert fresh.output_text == "answer-2" and fleet.paid == 2
                    assert witness.resume(fresh._exchange_id).output_text == "answer-2"
                    bound.finish("final source")

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
                                stream=True,
                                model="fixture",
                                input="same image",
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
                            witness_seed=1,
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
                exchange_id = next(iter(fleet.jobs))
                path = f"/exchanges/{exchange_id}/response.sse"
                async with http.get(
                    origin + path,
                    headers=hotkey_headers(other, b"", method="GET", path=path),
                ) as response:
                    assert response.status == 404
            finally:
                await registry.close()
                for task in fleet.tasks:
                    task.cancel()
                for task in fleet.tasks:
                    with suppress(asyncio.CancelledError):
                        await task
        await runner.cleanup()

    asyncio.run(scenario())
