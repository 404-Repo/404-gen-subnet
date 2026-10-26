"""Real HTTPS/WebSocket gateway, in-memory R2, simulated provider capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from bittensor_wallet import Keypair
from channel import Channel, WorkerChannel
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pod import PodRuntime
from support import MemoryState, Provider
from test_recording import PRICING
from witness_client import ReplayWitness, WitnessClient, WitnessError, run_generation
from witness_client.auth import hotkey_headers
from witness_client.calls import RequestKey, canonical
from witness_client.pods import Snapshot
from witness_client.request import serialize_body
from witness_client.tunnel import WebSocketTunnel

from pod_gateway.auth import Authenticator, Membership
from pod_gateway.budget import Budget
from pod_gateway.certs import pod_context
from pod_gateway.control import DrainControl, Registry
from pod_gateway.direct import Witness
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.records import Call, CallRecords
from pod_gateway.replay import export_bundle
from pod_gateway.security import capability
from pod_gateway.selection import Selection
from pod_gateway.server import Gateway
from pod_gateway.state import WriteUncertain

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
        "version": 1,
        "network": "finney",
        "netuid": 17,
        "block": 1,
        "fetched_at": now,
        "block_time": now,
        "hotkeys": [],
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
            "GET",
            "/pods/current",
            headers=headers,
            client_max_size=0,
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
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
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


class Harness:
    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch, *, native: bool = False) -> None:
        self.directory, self.monkeypatch, self.native = directory, monkeypatch, native
        self.key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
        self.scope = Scope("test", "test-0", self.key.ss58_address)
        self.state, self.provider = MemoryState(), Provider()
        self.records, self.selection = CallRecords(self.state), Selection(self.state)
        self.proof = (
            hashlib.sha256(
                (Path(__file__).resolve().parents[2] / "witness-server/verifier/params/verifying.bin").read_bytes()
            ).hexdigest()
            if native
            else PROOF
        )
        self.image = b"fixture prompt"
        self.runtimes: list[PodRuntime] = []
        self.services: list[Witness] = []
        self.runners: list[web.AppRunner] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.paid = self.gateway_bytes = self.origin_bytes = 0
        self.gateway_paths: list[str] = []
        self.origin_paths: list[str] = []
        self.failures: list[str] = []
        self.corrupt = False
        self.large = False
        self.dispatched, self.allow_response = threading.Event(), threading.Event()
        self.allow_response.set()
        self.origin = self.artifact_origin = ""
        self.registry: Registry
        self.allocator: Allocator
        self.server_tls: ssl.SSLContext

    def client(self, state: str = "client") -> WitnessClient:
        return WitnessClient(
            {
                "witness_url": self.origin,
                "proof_parameters": str(Path(__file__).resolve().parents[2] / "witness-server/verifier/params"),
                "state_directory": str(self.directory / state),
                "round": self.scope.round,
            },
            hotkey=self.key,
            openai_api_key="fixture-" + "a" * 80,
            delivery_timeout=180 if self.native else 5,
        )

    def exchange(self, tunnel: WebSocketTunnel, host: str, chunks: list[tuple[bool, bytes]], **kwargs: object) -> bytes:
        body = b"".join(chunk for _, chunk in chunks).partition(b"\r\n\r\n")[2]
        tunnel.send(len(body).to_bytes(8, "big") + body)
        tunnel.flush()
        response = bytearray()
        while True:
            chunk = tunnel.recv(timeout=10)
            if not chunk:
                return bytes(response)
            response.extend(chunk)

    def relay(
        self,
        store: MemoryState,
        prefix: str,
        deadline: float,
        directory: Path,
        verifier: object,
        channel: Channel,
        **kwargs: object,
    ) -> dict[str, object]:
        data = bytearray()
        while len(data) < 8 or len(data) < 8 + int.from_bytes(data[:8], "big"):
            data.extend(channel.recv(deadline))
        body = bytes(data[8:])
        failure = self.failures.pop(0) if self.failures else ""
        if failure == "before":
            raise TimeoutError("Fixture failed before dispatch")
        (directory / "authorization.json").write_bytes(b"{}")
        cast(Callable[[bytes], None], kwargs["before_release"])(body)
        if failure == "unknown":
            self.paid += 1
            raise TimeoutError("Fixture lost the provider outcome")
        with self.state.lock:
            self.paid += 1
            answer_number = self.paid
        self.dispatched.set()
        assert self.allow_response.wait(10)
        text = "answer-" + str(answer_number) + ("x" * (1024 * 1024) if self.large else "")
        response = canonical(
            {
                "type": "response.completed",
                "response": {
                    "id": "fixture",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            }
        )
        response = b"data: " + response + b"\n\n"
        for name, content in {
            "request.json": body,
            "response.sse": response,
            "client.bin": b"request ciphertext",
            "server.bin": b"response ciphertext",
            "authorization-server.bin": b"authorization ciphertext",
            "claim.json": b"{}",
            "authorization.json": b"{}",
            "handshake.json": b"{}",
        }.items():
            (directory / name).write_bytes(content)
            store.put(prefix + "/" + name, content)
        metadata = {
            **cast(dict[str, object], kwargs["extra_metadata"]),
            "round": self.scope.round,
            "hotkey": self.scope.hotkey,
            "exchange_id": directory.name,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "provider_status": 200,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        cast(Callable[[dict[str, object]], object], kwargs["seal"])(metadata)
        store.put(prefix + "/manifest.json", canonical(metadata))
        cast(Callable[[dict[str, object]], None], kwargs["before_delivery"])(metadata)
        if failure == "lost":
            cast(WorkerChannel, channel).socket.close()  # type: ignore[attr-defined]
            raise OSError("Fixture delivery was lost")
        channel.send(f"HTTP/1.1 200 OK\r\nContent-Length: {len(response)}\r\n\r\n".encode() + response)
        channel.finish()
        return metadata

    async def listen(self, app: web.Application, tls: ssl.SSLContext | None) -> str:
        runner = web.AppRunner(app, handler_cancellation=False)
        await runner.setup()
        self.runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
        await site.start()
        server = cast(asyncio.Server, site._server)
        assert server.sockets
        return f"{'https' if tls else 'http'}://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    async def launch(self, pod: Pod, body: dict[str, object]) -> None:
        spool = self.directory / pod.id / "spool"
        spool.parent.mkdir(mode=0o700, exist_ok=True)
        runtime = PodRuntime(
            evidence=self.state,
            records=self.records,
            budget=Budget(self.state, PRICING),
            scope=self.scope,
            start_id=pod.markers["WITNESS_START_ID"],
            pod_id=pod.id,
            token=capability(SECRET, self.scope, pod.markers["WITNESS_START_ID"]),
            origin=self.origin,
            proof_hash=self.proof,
            round_end=int(time.time()) + 600,
            spool=spool,
            selection=self.selection,
        )
        context, runtime.certificate = pod_context(spool.parent / "tls")
        self.runtimes.append(runtime)
        service = Witness(
            runtime, lambda key: self.artifact_origin + "/" + key
        )
        self.services.append(service)
        origin = await self.listen(service.application(), context)
        current = self.provider.get(pod.id)
        assert current is not None
        self.provider.pods[pod.id] = replace(current, tcp={8000: ("127.0.0.1", int(origin.rsplit(":", 1)[1]))})
        await asyncio.to_thread(runtime.run)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[Harness]:
        self.server_tls, client_tls = tls(self.directory)
        default = ssl.create_default_context

        def trust(
            purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
            *,
            cafile: str | None = None,
            capath: str | None = None,
            cadata: str | bytes | None = None,
        ) -> ssl.SSLContext:
            return default(purpose, cafile=cafile, capath=capath, cadata=cadata) if cadata or cafile else client_tls

        self.monkeypatch.setattr(ssl, "create_default_context", trust)
        if not self.native:
            self.monkeypatch.setattr(
                "witness_client.client.Prover", lambda _: SimpleNamespace(verifying_key_sha256=self.proof)
            )
            self.monkeypatch.setattr("witness_client.client.exchange", self.exchange)
            self.monkeypatch.setattr("pod.relay", self.relay)
        self.registry = Registry()
        self.allocator = Allocator(
            self.provider,
            DrainControl(self.registry),
            PodSpec("registry/witness@sha256:" + "a" * 64, capability_secret=SECRET),
            selection=self.selection,
        )
        membership = Membership("test", 17, self.scope.round)
        now = int(time.time() * 1000)
        membership.snapshot = {
            "version": 1,
            "network": "finney",
            "netuid": 17,
            "block": 1,
            "fetched_at": now,
            "block_time": now,
            "hotkeys": [self.scope.hotkey],
        }
        self.membership = membership
        self.gateway = Gateway(
            allocator=self.allocator,
            auth=Authenticator(membership),
            registry=self.registry,
            deployment=self.scope.deployment,
            round_number=self.scope.round,
            round_end=int(time.time()) + 600,
            secret=SECRET,
            proof_hash=self.proof,
            fence=lambda: True,
            pricing_hash=hashlib.sha256(PRICING).hexdigest(),
        )

        @web.middleware
        async def measure_gateway(request: web.Request, handler: Callable[[web.Request], object]) -> web.StreamResponse:
            self.gateway_paths.append(request.path)
            self.gateway_bytes += len(await request.read())
            response = await handler(request)  # type: ignore[misc]
            if isinstance(response, web.Response):
                self.gateway_bytes += len(cast(bytes, response.body) or b"")
            return response  # type: ignore[no-any-return]

        app = self.gateway.application()
        app.middlewares.insert(0, measure_gateway)
        self.origin = (await self.listen(app, self.server_tls)).replace("127.0.0.1", "localhost")

        async def artifact(request: web.Request) -> web.Response:
            self.origin_paths.append(request.path)
            assert "authorization" not in request.headers and "x-bittensor-hotkey" not in request.headers
            raw = self.state.get(request.path.removeprefix("/"))
            if raw is None:
                raise web.HTTPNotFound()
            self.origin_bytes += len(raw)
            return web.Response(body=b"corrupt" if self.corrupt else raw)

        artifacts = web.Application()
        artifacts.router.add_get("/{key:.*}", artifact)
        self.artifact_origin = (await self.listen(artifacts, self.server_tls)).replace("127.0.0.1", "localhost")
        loop = asyncio.get_running_loop()

        def created(pod: Pod, body: dict[str, object]) -> None:
            loop.call_soon_threadsafe(lambda: self.tasks.append(asyncio.create_task(self.launch(pod, body))))

        self.provider.on_create = created
        try:
            yield self
        finally:
            self.allow_response.set()
            for runtime in self.runtimes:
                runtime.drain()
            await self.registry.close()
            if self.tasks:
                await asyncio.wait_for(asyncio.gather(*self.tasks), 15)
            for runner in reversed(self.runners):
                await runner.cleanup()


@pytest.mark.parametrize("failure", ["", "before", "lost"])
def test_direct_call_deduplication_recovery_and_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.large, fixture.failures = True, [failure]
        async with fixture.running():
            def run() -> None:
                def generate(image: bytes, seed: int, client: WitnessClient, renderer: None) -> str:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        answers = list(pool.map(lambda _: client.create(model="fixture", input="x" * (1024 * 1024)), range(2)))
                    assert answers[0].output_text == answers[1].output_text
                    return answers[0].output_text
                with fixture.client() as witness:
                    expected = run_generation(generate, fixture.image, 42, witness, None)
                    assert fixture.paid == fixture.provider.creates == 1
                    identifier = exchange_id(witness)
                    assert witness.resume(identifier).output_text == expected
                    old_start = witness._gateway.ensure_pod()
                    execution = next(witness._state.glob("executions/*/*/execution.json"))
                    assert run_generation(generate, fixture.image, 42, witness, None) == expected
                with fixture.client("restarted") as witness:
                    assert witness.create(model="fixture", input="x" * (1024 * 1024)).output_text == expected
                    assert witness._gateway.ensure_pod() == old_start and fixture.paid == 1
                output = tmp_path / "bundle"
                export_bundle(records=fixture.records, evidence=fixture.state, scope=fixture.scope,
                    execution=execution, output=output, proof_hash=fixture.proof, pricing=PRICING,
                    verify=lambda _, path: json.loads((path / "manifest.json").read_bytes()))
                from witness_client import run_replay
                assert run_replay(generate, fixture.image, 42, ReplayWitness(output), None) == expected
                assert fixture.paid == 1
            await asyncio.wait_for(asyncio.to_thread(run), 45)
            assert fixture.gateway_bytes < 32 * 1024
            assert all(path.startswith(("/pods/", "/control/")) for path in fixture.gateway_paths)
    asyncio.run(scenario())



def test_direct_authorization_and_certificate_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                client = witness._gateway
                await asyncio.to_thread(client.ensure_pod)
                direct = client._service()
                path = "/status/00000000-0000-4000-8000-000000000001"
                other = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())

                def denied() -> None:
                    headers = {**hotkey_headers(other, b"", method="GET", path=path), **direct.headers}
                    response = direct.http.get(path, headers=headers, extensions={"sni_hostname": direct.server_name})
                    assert response.status_code == 403 and response.json()["error"] == "miner_not_assigned"
                    headers = {
                        **hotkey_headers(fixture.key, b"", method="GET", path=path),
                        "Authorization": "Bearer invalid",
                    }
                    response = direct.http.get(path, headers=headers, extensions={"sni_hostname": direct.server_name})
                    assert response.status_code == 401
                    direct.server_name = "different.internal"
                    direct.http.close()
                    assert direct.tls is not None
                    with httpx.Client(base_url=direct.http.base_url, verify=direct.tls, trust_env=False) as wrong:
                        direct.http = wrong
                        with pytest.raises(httpx.ConnectError):
                            direct._request_once("GET", path)

                await asyncio.to_thread(denied)
                assert fixture.paid == 0

    asyncio.run(scenario())


def test_renderer_payload_uses_direct_authenticated_pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            source = "export default function(THREE) {}"
            png = b"\x89PNG\r\n\x1a\n" + b"fixture"
            seen: list[str] = []

            async def backend(request: web.Request) -> web.Response:
                seen.append((await request.json())["source"])
                assert request.query["thetas"] == "24,120"
                if seen[-1] == "slow":
                    await asyncio.sleep(0.2)
                if seen[-1] == "rejected":
                    return web.json_response({"error": "Fixture rejection"}, status=422)
                return web.Response(body=png, content_type="image/png")

            app = web.Application()
            app.router.add_post("/render/grid", backend)
            origin = await fixture.listen(app, None)
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                fixture.services[0].renderer_url = origin + "/render/grid"
                await fixture.runners[0].cleanup()
                baseline = fixture.gateway_bytes

                def render() -> None:
                    with httpx.Client(transport=witness.renderer_transport()) as http:
                        url = "https://witness.internal/render/grid?thetas=24%2C120"
                        reply = http.post(url, json={"source": source})
                        assert reply.status_code == 200 and reply.content == png
                        reply = http.post(url, json={"source": "rejected"})
                        assert reply.status_code == 422 and reply.json() == {"error": "Fixture rejection"}
                        with pytest.raises(httpx.ReadTimeout):
                            http.post(url, json={"source": "slow"}, timeout=0.05)

                await asyncio.to_thread(render)
                assert seen == [source, "rejected", "slow"]
                assert fixture.gateway_bytes == baseline and fixture.paid == 0

    asyncio.run(scenario())


def test_unknown_dispatch_survives_client_and_pod_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.failures = ["unknown"]
        async with fixture.running():
            with fixture.client() as witness:
                gateway = witness._gateway
                reserve = gateway.reserve

                def short_wait(invocation: RequestKey) -> Snapshot:
                    value = reserve(invocation)
                    value.call.deadline = int(time.time()) + 2
                    return value

                monkeypatch.setattr(gateway, "reserve", short_wait)

                def unresolved() -> None:
                    with pytest.raises(WitnessError) as pending:
                        witness.create(model="fixture", input="image")
                    assert pending.value.code == "capture_failed"

                await asyncio.to_thread(unresolved)
                old = fixture.provider.list()[0]
                fixture.selection.update(old, active=False)
                fixture.provider.delete(old.id)
                gateway._expires = 0
                await asyncio.to_thread(gateway.ensure_pod)
                await asyncio.to_thread(unresolved)
                recording = fixture.records.load(fixture.scope, call_key().id)
                assert recording is not None
                located = fixture.records.locate(fixture.scope, recording.call.exchange_id)
                assert located is not None
                item, call = located
                summary = fixture.runtimes[-1].budget.summary(fixture.scope, item)
                assert fixture.paid == summary["possible_dispatches"] == summary["pending_usage"] == 1
                assert call.authorized and not call.not_dispatched

    asyncio.run(scenario())


def test_completed_call_remains_recoverable_after_round_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                result = await asyncio.to_thread(
                    witness.create, model="fixture", input="image"
                )
                fixture.services[0].round_end = fixture.runtimes[0].round_end = int(time.time()) - 1
                assert not fixture.runtimes[0].tick()
                recovered = await asyncio.to_thread(witness.resume, exchange_id(witness))
                assert recovered.output_text == result.output_text and fixture.paid == 1
                with pytest.raises(WitnessError) as closed:
                    await asyncio.to_thread(
                        witness.create, model="fixture", input="another image"
                    )
                assert closed.value.code == "round_closed" and fixture.paid == 1

    asyncio.run(scenario())


def test_direct_renderer_finishes_before_drain_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            started, release = asyncio.Event(), asyncio.Event()

            async def backend(request: web.Request) -> web.Response:
                started.set()
                await release.wait()
                return web.Response(body=b"render completed")

            app = web.Application()
            app.router.add_post("/render/grid", backend)
            origin = await fixture.listen(app, None)
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                fixture.services[0].renderer_url = origin + "/render/grid"

                def render() -> bytes:
                    with httpx.Client(transport=witness.renderer_transport()) as http:
                        return http.post("https://witness.internal/render/grid", json={"source": "fixture"}).content

                result = asyncio.create_task(asyncio.to_thread(render))
                await asyncio.wait_for(started.wait(), 5)
                runtime = fixture.runtimes[0]
                assert runtime.rendering == 1
                runtime.drain()
                assert not runtime.tick()
                release.set()
                assert await asyncio.wait_for(result, 5) == b"render completed"
                assert runtime.rendering == 0 and runtime.tick()

    asyncio.run(scenario())


def test_deliberate_replacement_waits_for_capture_without_repeating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                gateway = witness._gateway
                gateway.delivery_timeout = gateway._allocation.delivery_timeout = 20
                old_start = await asyncio.to_thread(gateway.ensure_pod)
                fixture.allow_response.clear()
                call = asyncio.create_task(
                    asyncio.to_thread(witness.create, model="fixture", input="image")
                )
                assert await asyncio.to_thread(fixture.dispatched.wait, 5)
                replacement = await asyncio.to_thread(
                    fixture.allocator.acquire, fixture.scope, "00000000-0000-4000-8000-000000000002", replaces=old_start
                )
                assert replacement.phase == "draining" and fixture.provider.creates == 1
                assert not fixture.runtimes[0].selected() and not fixture.runtimes[0].tick()
                fixture.allow_response.set()
                result = await asyncio.wait_for(call, 25)
                gateway._expires = 0
                assert await asyncio.to_thread(gateway.ensure_pod) != old_start
                assert (await asyncio.to_thread(witness.resume, exchange_id(witness))).output_text == result.output_text
                assert fixture.provider.creates == 2 and fixture.paid == 1

    asyncio.run(scenario())


def test_concurrent_direct_calls_retain_distinct_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                fixture.allow_response.clear()
                calls = [asyncio.create_task(asyncio.to_thread(witness.create, model="fixture", input="image", request_id=i))
                         for i in (0, 1)]
                deadline = time.monotonic() + 10
                while fixture.paid != 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert fixture.paid == len(fixture.runtimes[0].active) == 2
                fixture.allow_response.set()
                results = await asyncio.wait_for(asyncio.gather(*calls), 10)
                assert {result.output_text for result in results} == {"answer-1", "answer-2"}
                assert fixture.paid == 2
    asyncio.run(scenario())



def test_call_waits_for_capture_capacity_without_restarting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                fixture.records.exchange_seconds = 5
                bound = witness
                other = witness
                rejected = threading.Event()
                start_capture = fixture.runtimes[0].start_capture

                def admit(invocation: RequestKey, call: Call, channel: Channel) -> bool:
                    started = start_capture(invocation, call, channel)
                    if not started:
                        rejected.set()
                    return started

                monkeypatch.setattr(fixture.runtimes[0], "start_capture", admit)
                fixture.allow_response.clear()
                calls = [asyncio.create_task(asyncio.to_thread(
                    bound.create, model="fixture", input="image", request_id=i,
                )) for i in (0, 1)]
                deadline = time.monotonic() + 4
                while fixture.paid != 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert fixture.paid == 2
                calls.append(asyncio.create_task(asyncio.to_thread(
                    other.create, model="fixture", input="other image",
                )))
                assert await asyncio.to_thread(rejected.wait, 3)
                fixture.allow_response.set()
                answers = await asyncio.wait_for(asyncio.gather(*calls), 10)
                assert {answer.output_text for answer in answers} == {"answer-1", "answer-2", "answer-3"}
                assert fixture.paid == 3 and fixture.provider.creates == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
def test_lost_claim_acknowledgement_recovers_without_repeating_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                fixture.records.exchange_seconds = 5
                claim, lost = fixture.records.claim, False

                def uncertain(scope: Scope, invocation: RequestKey, pod_id: str, capture_id: str) -> bool:
                    nonlocal lost
                    if not lost:
                        lost = True
                        if committed:
                            claim(scope, invocation, pod_id, capture_id)
                        raise WriteUncertain("Fixture lost the claim acknowledgement")
                    return claim(scope, invocation, pod_id, capture_id)

                monkeypatch.setattr(fixture.records, "claim", uncertain)
                bound = witness
                answer = await asyncio.wait_for(asyncio.to_thread(bound.create, model="fixture", input="image"), 10)
                assert answer.output_text == "answer-1" and fixture.paid == 1
                recording = fixture.records.load(fixture.scope, call_key().id)
                assert recording is not None
                located = fixture.records.locate(fixture.scope, exchange_id(witness))
                assert located is not None
                summary = fixture.runtimes[0].budget.summary(fixture.scope, located[0])
                assert summary["possible_dispatches"] == 1 and summary["pending_usage"] == 0

    asyncio.run(scenario())


def test_direct_upload_waits_for_a_slow_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        entered, resume = threading.Event(), threading.Event()
        async with fixture.running():
            with fixture.client() as witness:
                gateway = witness._gateway
                start_id = await asyncio.to_thread(gateway.ensure_pod)
                runtime = fixture.runtimes[0]
                capture = runtime._capture

                def delayed(invocation: RequestKey, call: Call, channel: Channel) -> None:
                    entered.set()
                    assert resume.wait(5)
                    capture(invocation, call, channel)

                monkeypatch.setattr(runtime, "_capture", delayed)
                body = serialize_body({"model": "fixture", "input": "x" * (5 * 1024 * 1024), "stream": True}, "api")
                invocation = RequestKey.from_body("api", body)
                await asyncio.to_thread(gateway.reserve, invocation)
                snapshot = await asyncio.to_thread(gateway.dispatch, invocation, start_id)

                def upload() -> bytes:
                    with gateway.tunnel(snapshot.call.exchange_id) as tunnel:
                        return fixture.exchange(tunnel, "fixture", [(False, b"POST / HTTP/1.1\r\n\r\n" + body)])

                task = asyncio.create_task(asyncio.to_thread(upload))
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    await asyncio.sleep(0.2)
                finally:
                    resume.set()
                raw = await asyncio.wait_for(task, 10)
                assert b"answer-1" in raw and fixture.paid == 1
                located = fixture.records.locate(fixture.scope, snapshot.call.exchange_id)
                assert located is not None and located[1].phase == "issued"

    asyncio.run(scenario())


@pytest.mark.parametrize("status,error", [(401, "allocation_access_expired"), (409, "pod_not_selected")])
def test_next_render_resolves_a_stale_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, error: str,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            rendered: list[str] = []
            png = b"\x89PNG\r\n\x1a\nfixture"

            async def backend(request: web.Request) -> web.Response:
                rendered.append((await request.json())["source"])
                return web.Response(body=png, content_type="image/png")

            app = web.Application()
            app.router.add_post("/render/grid", backend)
            origin = await fixture.listen(app, None)
            with fixture.client() as witness:
                gateway = witness._gateway
                await asyncio.to_thread(gateway.ensure_pod)
                fixture.services[0].renderer_url = origin + "/render/grid"
                service = gateway._service()
                with httpx.Client(base_url=service.http.base_url, transport=httpx.MockTransport(
                    lambda _: httpx.Response(status, json={"error": error}),
                )) as stale:
                    service.http = stale

                    def render_twice() -> None:
                        with httpx.Client(transport=witness.renderer_transport()) as http:
                            assert http.post("https://witness.internal/render/grid", json={"source": "first"}).status_code == status
                            reply = http.post("https://witness.internal/render/grid", json={"source": "second"})
                            assert reply.status_code == 200 and reply.content == png

                    await asyncio.to_thread(render_twice)
                assert rendered == ["second"] and fixture.provider.creates == 1

    asyncio.run(scenario())


def test_direct_upgrade_during_tls_session_ticket_processing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.sync.client import connect
    from witness_client.gateway import GatewayClient

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
            if direction == "read" and content_type == 22 and message_type == 4:
                reading_ticket.set()
                assert sent_upgrade.wait(2)

        def send(socket: ssl.SSLSocket, data: bytes, flags: int = 0) -> int:
            if not bytes(data).startswith(b"GET "):
                return original_send(socket, data, flags)
            reading_ticket.wait(0.1)
            try:
                return original_send(socket, data, flags)
            finally:
                sent_upgrade.set()

        monkeypatch.setattr(client_tls, "_msg_callback", ticket)
        monkeypatch.setattr(ssl.SSLSocket, "send", send)
        monkeypatch.setattr(
            "witness_client.gateway.connect", lambda uri, **options: connect(uri, **{**options, "open_timeout": 1})
        )

        async def echo(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_str(await socket.receive_str())
            return socket

        runner = web.AppRunner(web.Application())
        runner.app.router.add_get("/exchanges/{exchange_id}/connect", echo)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
        await site.start()
        server = cast(asyncio.Server, site._server)
        assert server.sockets

        def run() -> None:
            with httpx.Client(base_url=f"https://localhost:{server.sockets[0].getsockname()[1]}") as http:
                gateway = GatewayClient(http, Keypair.create_from_mnemonic(Keypair.generate_mnemonic()), tmp_path, 1)
                gateway.tls = client_tls
                gateway.server_name = "localhost"
                with gateway.tunnel("fixture") as tunnel:
                    tunnel.socket.send("upgrade arrived")
                    assert tunnel.socket.recv(timeout=1) == "upgrade arrived"

        try:
            await asyncio.wait_for(asyncio.to_thread(run), 5)
            assert reading_ticket.is_set()
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_allocated_calls_continue_without_gateway_and_normal_completion_skips_artifact_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.large = True
        async with fixture.running():
            with fixture.client() as witness:
                gateway = witness._gateway
                await asyncio.to_thread(gateway.ensure_pod)
                await fixture.runners[0].cleanup()
                baseline = fixture.gateway_bytes
                result = await asyncio.to_thread(
                    witness.create, model="fixture", input="x" * (1024 * 1024)
                )
                assert len(result.output_text) > 1024 * 1024
                assert fixture.gateway_bytes == baseline
                assert fixture.origin_bytes == 0
                assert fixture.paid == fixture.provider.creates == 1
                record_property("gateway_bytes_after_allocation", fixture.gateway_bytes - baseline)
                record_property("normal_artifact_payload_bytes", fixture.origin_bytes)
                record_property("response_text_bytes", len(result.output_text))

    asyncio.run(scenario())


@pytest.mark.parametrize("corrupt", [False, True])
def test_restart_and_replacement_use_original_evidence_and_deny_stale_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt: bool,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                generation = witness
                first = await asyncio.to_thread(generation.create, model="fixture", input="image")
                current = fixture.provider.list()[0]
                fixture.selection.update(current, active=False)
                old = fixture.runtimes[0]
                assert not old.selected()
                fixture.provider.delete(current.id)
                witness._gateway._expires = 0
                await asyncio.to_thread(witness._gateway.ensure_pod)
                assert len(fixture.runtimes) == 2 and fixture.provider.creates == 2
                fixture.corrupt = corrupt
                if corrupt:
                    with pytest.raises(WitnessError, match="hash differs"):
                        await asyncio.to_thread(witness.resume, exchange_id(witness))
                else:
                    assert (
                        await asyncio.to_thread(witness.resume, exchange_id(witness))
                    ).output_text == first.output_text
                assert fixture.paid == 1
                new_item = RequestKey.from_body(
                        "api", serialize_body({"model": "fixture", "input": "new", "stream": True}, "api")
                    )
                call = fixture.records.reserve(fixture.scope, new_item)
                assigned = fixture.records.assign(fixture.scope, new_item, old.start_id, old.pod_id)
                assert not old.start_capture(new_item, assigned, cast(Channel, None))
                located = fixture.records.locate(fixture.scope, call.exchange_id)
                assert located is not None and located[1].phase == "assigned"

    asyncio.run(scenario())


def call_key() -> RequestKey:
    return RequestKey.from_body("api", serialize_body({"model": "fixture", "input": "image", "stream": True,
                                                       "service_tier": "default"}, "api"))


def exchange_id(witness: WitnessClient) -> str:
    return max(witness._state.glob("*/outcome.json"), key=lambda path: path.stat().st_mtime_ns).parent.name
