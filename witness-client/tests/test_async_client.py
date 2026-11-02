"""Async calls retain Pod identity, evidence and recovery across cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Literal, cast

import httpx
import openai
import pytest
from bittensor_wallet import Keypair

from witness_client import AsyncWitnessClient, WitnessClient, WitnessConfig, WitnessError
from witness_client.async_pods import AsyncSignedTransport
from witness_client.async_tunnel import AsyncWebSocketTunnel
from witness_client.calls import RequestKey, canonical
from witness_client.gateway import SignedTransport
from witness_client.pods import CallView, Snapshot
from witness_client.response import AsyncModelStream
from witness_client.types import CredentialProof, EarlyDisclosure, PrivateRecord
from test_client_api import config as config, event
from test_async_transport import tls_context as tls_context


@pytest.fixture(autouse=True)
def isolated_async_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class ProverStub:
        verifying_key_sha256 = "a" * 64

        def __init__(self, parameters: Path) -> None:
            pass

        def prove(self, private: list[PrivateRecord]) -> CredentialProof:
            return cast(CredentialProof, {})

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Async SDK tests must not contact a service")

    monkeypatch.setattr("witness_client.async_client.Prover", ProverStub)
    monkeypatch.setattr("witness_client.client.Prover", ProverStub)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


class PodService:
    def __init__(
        self,
        hotkey: Keypair,
        body: bytes,
        *,
        phase: Literal["assigned", "capturing", "issued"] = "issued",
    ) -> None:
        self.hotkey, self.body, self.phase = hotkey, body, phase
        self.response = event(
            {
                "id": "resp_fixture",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }
        )
        self.status = 200
        self.calls: dict[str, Snapshot] = {}
        self.starts: list[str] = []
        self.downloads: list[str] = []
        self.polling = asyncio.Event()
        self.block_poll = False

    def complete(self, identifier: str) -> None:
        snapshot = self.calls[identifier]
        manifest = canonical(
            {
                "call_id": snapshot.call.request.id,
                "exchange_id": identifier,
                "proof_key_sha256": "a" * 64,
                "request_key": snapshot.call.request.model_dump(),
                "request_sha256": hashlib.sha256(self.body).hexdigest(),
                "response_sha256": hashlib.sha256(self.response).hexdigest(),
                "hotkey": self.hotkey.ss58_address,
                "round": "test-0",
                "provider_status": self.status,
            }
        )
        if snapshot.session is None:
            snapshot.session = {
                "id": identifier,
                "protocol": 2,
                "proof_key_sha256": "a" * 64,
                "provider": "https://api.openai.com/v1/responses",
                "miner": self.hotkey.ss58_address,
                "round": "test-0",
            }
        snapshot.call.phase = "issued"
        snapshot.call.manifest_sha256 = hashlib.sha256(manifest).hexdigest()
        snapshot.completion = {
            "manifest": manifest.decode(),
            "session": snapshot.session,
            "artifacts": {
                name: {
                    "url": f"https://evidence.test/{name}",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for name, data in [("request.json", self.body), ("response.sse", self.response)]
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "evidence.test":
            assert "authorization" not in request.headers
            assert "x-bittensor-hotkey" not in request.headers
            self.downloads.append(request.url.path)
            return httpx.Response(
                200, content=self.body if request.url.path == "/request.json" else self.response
            )
        assert request.headers["authorization"] == "Bearer pod-access"
        path = request.url.raw_path.decode()
        payload = "\n".join(
            [
                "btauth/1",
                "sr25519",
                request.method,
                path,
                hashlib.sha256(request.content).hexdigest(),
                request.headers["x-bittensor-nonce"],
                self.hotkey.ss58_address,
                "404-witness",
            ]
        )
        assert self.hotkey.verify(
            payload.encode(), bytes.fromhex(request.headers["x-bittensor-signature"][2:])
        )
        assert request.extensions["sni_hostname"] == "witness.internal"
        if request.method == "POST":
            invocation = RequestKey.model_validate(json.loads(request.content)["request"])
            self.starts.append(invocation.id)
            identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, invocation.id))
            if identifier not in self.calls:
                self.calls[identifier] = Snapshot(
                    call=CallView(
                        request=invocation,
                        exchange_id=identifier,
                        deadline=None if self.phase == "assigned" else int(time.time()) + 60,
                        phase=self.phase,
                        authorized=self.phase == "capturing",
                    ),
                    hotkey=self.hotkey.ss58_address,
                    session={
                        "id": identifier,
                        "protocol": 2,
                        "proof_key_sha256": "a" * 64,
                        "provider": (
                            "https://api.openai.com/v1/responses"
                            if invocation.provider == "api"
                            else "https://chatgpt.com/backend-api/codex/responses"
                        ),
                        "miner": self.hotkey.ss58_address,
                        "round": "test-0",
                    },
                )
                if self.phase == "issued":
                    self.complete(identifier)
                elif self.phase == "capturing":
                    self.calls[identifier].session = None
        else:
            identifier = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json=self.calls[identifier].model_dump())

    async def async_handle(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        if request.method == "GET" and request.url.host != "evidence.test":
            self.polling.set()
            if self.block_poll:
                await asyncio.Event().wait()
        return self.handle(request)

    async def connect(self, client: AsyncWitnessClient) -> None:
        await client._http.aclose()
        await client._gateway._artifacts.http.aclose()
        client._http = httpx.AsyncClient(
            base_url="https://witness.test", transport=httpx.MockTransport(self.async_handle)
        )
        client._gateway.http = client._http
        client._gateway._direct = AsyncSignedTransport(
            client._http,
            self.hotkey,
            5,
            headers={"Authorization": "Bearer pod-access"},
            server_name="witness.internal",
        )
        client._gateway._expires = int(time.time()) + 3600
        client._gateway._start_id = str(uuid.uuid4())
        client._gateway.round_number = "test-0"
        client._gateway._artifacts.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.async_handle)
        )


@pytest.mark.parametrize("phase", ["capturing", "failed"])
def test_async_failed_capture_allows_same_id_retry(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, phase: Literal["capturing", "failed"],
) -> None:
    async def scenario() -> None:
        key = Keypair.create_from_uri("//Alice")
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        current: Snapshot | None = None

        async def start(
            invocation: RequestKey, *, expected_exchange_id: str | None = None, deadline: float | None = None,
        ) -> Snapshot:
            nonlocal current
            if current is None:
                current = Snapshot(
                    call=CallView(request=invocation, exchange_id=first, deadline=int(time.time()) + 60,
                                  phase=phase, authorized=True),
                    hotkey=key.ss58_address, session=None,
                )
            elif current.call.phase == "failed":
                current = current.model_copy(deep=True)
                current.call.exchange_id, current.call.phase = second, "issued"
            return current

        async def snapshot(identifier: str, **kwargs: object) -> Snapshot:
            assert current is not None
            if current.call.exchange_id == first:
                current.call.phase = "failed"
            return current

        async def recorded(*args: object, **kwargs: object) -> tuple[object, int, bytes]:
            return {}, 200, event({"id": "retry", "status": "completed", "output": []})

        async with AsyncWitnessClient(config, hotkey=key, openai_api_key="x" * 80) as witness:
            monkeypatch.setattr(witness._gateway, "start", start)
            monkeypatch.setattr(witness._gateway, "snapshot", snapshot)
            monkeypatch.setattr(witness._gateway, "recorded", recorded)
            with pytest.raises(openai.APIConnectionError):
                await witness.create(model="fixture", input="image", request_id=7)
            assert (await witness.create(model="fixture", input="image", request_id=7)).id == "retry"
            assert (await witness.create(model="fixture", input="image", request_id=7)).id == "retry"

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["api", "codex"])
def test_async_create_stream_request_ids_and_sync_restart_reuse_saved_evidence(
    config: WitnessConfig,
    mode: Literal["api", "codex"],
) -> None:
    key = Keypair.create_from_uri("//Alice")
    session_id = "fixture-session" if mode == "codex" else None

    async def run() -> PodService:
        async with AsyncWitnessClient(
            config,
            hotkey=key,
            openai_api_key="x" * 80 if mode == "api" else None,
            subscription="codex" if mode == "codex" else None,
        ) as client:
            invocation, body = client._request(
                7, session_id, {"model": "fixture", "input": "image"}
            )
            service = PodService(key, body)
            await service.connect(client)
            first, second = await asyncio.gather(
                *[
                    client.responses.create(
                        model="fixture", input="image", request_id=7, session_id=session_id
                    )
                    for _ in range(2)
                ]
            )
            assert first.id == second.id == "resp_fixture"
            assert first.usage is not None and first.usage.total_tokens == 5
            assert service.starts == [invocation.id, invocation.id]
            identifier = next(iter(service.calls))
            assert (await client.resume(identifier)).id == first.id
            assert (await client.saved_response(identifier)).body == service.response
            assert (await client.capture_context()).provider == mode
            async with await client.responses.create(
                model="fixture",
                input="image",
                request_id=7,
                session_id=session_id,
                stream=True,
            ) as stream:
                assert [item.type async for item in stream] == ["response.completed"]
            await client.create(model="fixture", input="image", request_id=8, session_id=session_id)
            assert len(service.calls) == 2
            assert invocation.session_id == session_id
            assert json.loads(body)["stream"] is True
            if mode == "codex":
                assert json.loads(body)["store"] is False
        assert client._http.is_closed and client._gateway._artifacts.http.is_closed
        return service

    service = asyncio.run(run())
    with WitnessClient(
        config,
        hotkey=key,
        openai_api_key="x" * 80 if mode == "api" else None,
        subscription="codex" if mode == "codex" else None,
    ) as sync:
        with httpx.Client(
            base_url="https://witness.test", transport=httpx.MockTransport(service.handle)
        ) as http:
            sync._gateway._direct = SignedTransport(
                http,
                key,
                5,
                headers={"Authorization": "Bearer pod-access"},
                server_name="witness.internal",
            )
            sync._gateway._expires = int(time.time()) + 3600
            sync._gateway._start_id = str(uuid.uuid4())
            sync._gateway._artifacts.http.close()
            sync._gateway._artifacts.http = httpx.Client(
                transport=httpx.MockTransport(service.handle)
            )
            assert (
                sync.responses.create(
                    model="fixture", input="image", request_id=7, session_id=session_id
                ).id
                == "resp_fixture"
            )
    assert len(service.calls) == 2


@pytest.mark.parametrize("status", [200, 400, 429, 500])
def test_async_recovery_preserves_provider_errors_and_rejects_changed_evidence(
    config: WitnessConfig,
    status: int,
) -> None:
    async def run() -> None:
        key = Keypair.create_from_uri("//Alice")
        async with AsyncWitnessClient(config, hotkey=key, openai_api_key="x" * 80) as client:
            _, body = client._request(0, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body)
            service.status = status
            if status != 200:
                service.response = b'{"error":{"message":"fixture rejection","code":"fixture"}}'
            await service.connect(client)
            if status == 200:
                assert (
                    await client.responses.create(model="fixture", input="image")
                ).id == "resp_fixture"
            else:
                with pytest.raises(openai.APIStatusError) as error:
                    await client.responses.create(model="fixture", input="image")
                assert error.value.status_code == status
            assert len(service.calls) == 1
            service.response += b"changed"
            with pytest.raises(openai.APIConnectionError) as invalid:
                await client.create(model="fixture", input="image")
            assert isinstance(invalid.value.__cause__, WitnessError)
            assert invalid.value.__cause__.code == "invalid_recording"
            assert len(service.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("waiting_for", ["capture", "preparation"])
def test_async_queued_call_can_finish_after_waiting_longer_than_a_capture_window(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, waiting_for: str,
) -> None:
    client_config: WitnessConfig = request.getfixturevalue("config")
    async def run() -> None:
        waiting = asyncio.Event()
        started = time.monotonic()
        wall = time.time()
        clock = [started]
        monkeypatch.setattr("witness_client.async_client.time", SimpleNamespace(
            monotonic=lambda: clock[0], time=lambda: wall + clock[0] - started,
        ))

        class WaitingSemaphore(asyncio.BoundedSemaphore):
            async def __aenter__(self) -> None:
                waiting.set()
                await super().__aenter__()

        gate = WaitingSemaphore(1)
        await gate.acquire()
        key = Keypair.create_from_uri("//Alice")
        async with AsyncWitnessClient(client_config, hotkey=key, openai_api_key="x" * 80) as client:
            monkeypatch.setattr(client, "_capture_slots" if waiting_for == "capture" else "_preparation", gate)
            _, body = client._request(0, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body, phase="assigned")
            await service.connect(client)

            class Socket:
                async def send(self, message: str | bytes) -> None:
                    pass

                async def recv(self) -> bytes:
                    return b""

                async def close(self) -> None:
                    pass

            @asynccontextmanager
            async def tunnel(identifier: str, *, deadline: float) -> AsyncIterator[AsyncWebSocketTunnel]:
                yield AsyncWebSocketTunnel(Socket(), identifier)

            async def exchange(tunnel: AsyncWebSocketTunnel, *args: object, **kwargs: object) -> bytes:
                clock[0] += 18 + 45
                service.complete(tunnel.identifier)
                return (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(service.response)}\r\n\r\n".encode()
                    + service.response
                )

            monkeypatch.setattr(client._gateway, "tunnel", tunnel)
            monkeypatch.setattr("witness_client.async_client.async_exchange", exchange)
            queued = asyncio.create_task(client.create(model="fixture", input="image"))
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                clock[0] += 1801
            finally:
                gate.release()
            assert (await asyncio.wait_for(queued, 3)).id == "resp_fixture"

    asyncio.run(run())


def test_cancelled_paid_capture_is_recovered_after_restart_without_another_exchange(
    config: WitnessConfig,
) -> None:
    async def run() -> None:
        key = Keypair.create_from_uri("//Alice")
        async with AsyncWitnessClient(config, hotkey=key, openai_api_key="x" * 80) as first:
            _, body = first._request(0, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body, phase="capturing")
            service.block_poll = True
            await service.connect(first)
            task = asyncio.create_task(first.responses.create(model="fixture", input="image"))
            await asyncio.wait_for(service.polling.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            identifier = next(iter(service.calls))
        service.block_poll = False
        service.complete(identifier)
        async with AsyncWitnessClient(config, hotkey=key, openai_api_key="x" * 80) as second:
            await service.connect(second)
            assert (await second.resume(identifier)).id == "resp_fixture"
            assert (
                await second.responses.create(model="fixture", input="image")
            ).id == "resp_fixture"
        assert len(service.calls) == 1 and len(set(service.starts)) == 1
        assert service.downloads.count("/request.json") == 1

    asyncio.run(run())


def test_cancellation_during_native_proof_closes_tunnel_and_never_authorizes_later(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = Event()
    finished = Event()
    sent: list[str] = []
    closed: list[str] = []

    async def run() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        key = Keypair.create_from_uri("//Alice")
        async with AsyncWitnessClient(
            config, hotkey=key, openai_api_key="x" * 80, parallel_preparations=1
        ) as client:
            _, body = client._request(0, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body, phase="assigned")
            await service.connect(client)

            class Socket:
                async def send(self, message: str | bytes) -> None:
                    assert isinstance(message, str)
                    sent.append(json.loads(message)["type"])

                async def recv(self) -> str:
                    return '{"type":"status","status":"running","handshook":true,"authorized":true}'

                async def close(self) -> None:
                    pass

            @asynccontextmanager
            async def tunnel(
                identifier: str, *, deadline: float
            ) -> AsyncIterator[AsyncWebSocketTunnel]:
                try:
                    yield AsyncWebSocketTunnel(Socket(), identifier)
                finally:
                    closed.append(identifier)

            def prove(private: list[PrivateRecord]) -> CredentialProof:
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert released.wait(3)
                    return cast(CredentialProof, {})
                finally:
                    finished.set()

            async def exchange(
                tunnel: AsyncWebSocketTunnel, *args: object, **kwargs: object
            ) -> bytes:
                disclosure = cast(
                    EarlyDisclosure,
                    {
                        "private": [],
                        "disclosures": {"client": [], "server": []},
                        "response_key": {},
                        "client_bytes": 1,
                        "probe": True,
                    },
                )
                from collections.abc import Awaitable, Callable

                await cast(Callable[[EarlyDisclosure], Awaitable[None]], kwargs["authorize"])(
                    disclosure
                )
                pytest.fail("Cancelled native proof must not continue to inference")

            monkeypatch.setattr(client._gateway, "tunnel", tunnel)
            prover = await client._get_prover()
            monkeypatch.setattr(prover, "prove", prove)
            monkeypatch.setattr("witness_client.async_client.async_exchange", exchange)
            task = asyncio.create_task(client.responses.create(model="fixture", input="image"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert closed == list(service.calls) and sent == []
                # Cancellation must also release the call's preparation slot.
                async with asyncio.timeout(1):
                    async with client._preparation:
                        pass
            finally:
                released.set()
                assert await asyncio.to_thread(finished.wait, 2)
            await asyncio.sleep(0)
            assert sent == []

    asyncio.run(run())


def test_cancelled_duplicate_wait_does_not_cancel_the_original_capture(
    config: WitnessConfig,
) -> None:
    async def run() -> None:
        key = Keypair.create_from_uri("//Alice")
        async with AsyncWitnessClient(config, hotkey=key, openai_api_key="x" * 80) as client:
            _, body = client._request(0, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body, phase="capturing")
            service.block_poll = True
            await service.connect(client)
            original = asyncio.create_task(client.create(model="fixture", input="image"))
            duplicate = asyncio.create_task(client.create(model="fixture", input="image"))
            await asyncio.wait_for(service.polling.wait(), 2)
            duplicate.cancel()
            with pytest.raises(asyncio.CancelledError):
                await duplicate
            assert not original.done() and len(service.starts) == 1
            original.cancel()
            with pytest.raises(asyncio.CancelledError):
                await original

    asyncio.run(run())


def test_async_stream_detects_truncation_and_close() -> None:
    from witness_client.response import ModelStream

    async def run() -> None:
        stream = AsyncModelStream(ModelStream(b'data: {"type":"response.created"}\n\n', "api"))
        with pytest.raises(openai.APIError, match="Missing completed"):
            _ = [value async for value in stream]
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run())


def test_cancelled_pod_startup_recovers_the_remote_allocation_and_renderer_grant(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    tls_context: tuple[object, Path],
) -> None:
    import ssl
    from typing import Any
    from witness_client.async_pods import AsyncPodGatewayClient

    async def run() -> None:
        key = Keypair.create_from_uri("//Alice")
        state = Path(config["state_directory"])
        state.mkdir(mode=0o700)
        polling = asyncio.Event()
        starts: list[str] = []
        session: dict[str, object] | None = None
        block_poll = True
        expired_render = True
        connections: list[httpx.AsyncClient] = []
        http_type = httpx.AsyncClient

        def reply() -> dict[str, object]:
            result: dict[str, object] = {"round": "test-0", "session": session, "retry_after": 0}
            if session is not None and session["phase"] == "allocated":
                result["witness"] = {
                    "address": "192.0.2.1",
                    "port": 443,
                    "server_name": "witness.internal",
                    "pod_id": "fixture-pod",
                    "start_id": session["start_id"],
                    "expires": int(time.time()) + 3600,
                    "access": "pod-access",
                    "certificate": tls_context[1].read_text(),
                }
            return result

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal session, expired_render
            assert request.headers["x-bittensor-hotkey"] == key.ss58_address
            if request.url.host == "witness.test":
                assert "authorization" not in request.headers
                assert "Prefer" in request.headers
                if request.method == "POST":
                    assert request.url.path == "/v1/pods/start"
                    start_id = json.loads(request.content)["start_id"]
                    starts.append(start_id)
                    session = {"start_id": start_id, "phase": "starting", "pod_id": "fixture-pod"}
                elif request.url.path != "/v1/pods/current":
                    assert session is not None
                    assert request.url.path == f"/v1/pods/{session['start_id']}"
                    polling.set()
                    if block_poll:
                        await asyncio.Event().wait()
                    session["phase"] = "allocated"
                return httpx.Response(200, json=reply())
            assert request.url.host == "192.0.2.1"
            assert request.headers["authorization"] == "Bearer pod-access"
            assert request.extensions["sni_hostname"] == "witness.internal"
            assert request.url.raw_path == b"/v1/render/grid?thetas=30&lighting=neutral"
            assert request.extensions["timeout"] == {
                name: 7 for name in ("connect", "read", "write", "pool")
            }
            assert json.loads(request.content) == {"source": "fixture source"}
            payload = "\n".join(
                [
                    "btauth/1",
                    "sr25519",
                    request.method,
                    request.url.raw_path.decode(),
                    hashlib.sha256(request.content).hexdigest(),
                    request.headers["x-bittensor-nonce"],
                    key.ss58_address,
                    "404-witness",
                ]
            )
            assert key.verify(
                payload.encode(), bytes.fromhex(request.headers["x-bittensor-signature"][2:])
            )
            if expired_render:
                expired_render = False
                return httpx.Response(401, json={"error": "allocation_access_expired"})
            return httpx.Response(200, content=b"fixture PNG")

        def http_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            context = kwargs.get("verify")
            if isinstance(context, ssl.SSLContext):
                assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            kwargs["transport"] = httpx.MockTransport(handler)
            http = http_type(*args, **kwargs)
            connections.append(http)
            return http

        monkeypatch.setattr("witness_client.async_pods.httpx.AsyncClient", http_client)
        async with http_client(base_url="https://witness.test") as http:
            gateway = AsyncPodGatewayClient(http, key, state, 5)
            task = asyncio.create_task(gateway.ensure_pod())
            try:
                await asyncio.wait_for(polling.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                await gateway.aclose()
        block_poll = False
        async with http_client(base_url="https://witness.test") as http:
            restarted = AsyncPodGatewayClient(http, key, state, 5)
            try:
                assert await restarted.ensure_pod() == starts[0]
                async with http_type(transport=httpx.MockTransport(restarted.send)) as renderer:
                    url = "https://witness.internal/v1/render/grid?thetas=30&lighting=neutral"
                    assert (
                        await renderer.post(url, json={"source": "fixture source"}, timeout=7)
                    ).status_code == 401
                    assert (
                        await renderer.post(url, json={"source": "fixture source"}, timeout=7)
                    ).content == b"fixture PNG"
                assert len(restarted._connections) == 2
            finally:
                await restarted.aclose()
        assert len(starts) == 1 and all(http.is_closed for http in connections)

    asyncio.run(run())


@pytest.mark.parametrize("recovered", [False, True])
def test_async_safe_replacement_keeps_the_existing_retry_bound(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    recovered: bool,
) -> None:
    from types import SimpleNamespace
    from witness_client.types import Session

    async def run() -> None:
        key = Keypair.create_from_uri("//Alice")
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        starts: list[str | None] = []
        clock = [1000.0]

        async def start(
            invocation: RequestKey,
            *,
            expected_exchange_id: str | None = None,
            deadline: float | None = None,
        ) -> Snapshot:
            starts.append(expected_exchange_id)
            ready = recovered and expected_exchange_id is not None
            return Snapshot(
                call=CallView(
                    request=invocation,
                    exchange_id=second if ready else first,
                    deadline=2000 if ready else 900,
                    phase="issued" if ready else "failed",
                    not_dispatched=not ready,
                ),
                hotkey=key.ss58_address,
                session=None,
            )

        async def sleep(seconds: float) -> None:
            clock[0] += seconds
            await asyncio.sleep(0)

        async def recorded(*args: object, **kwargs: object) -> tuple[Session, int, bytes]:
            return (
                cast(Session, {}),
                200,
                event({"id": "resp_recovered", "status": "completed", "output": []}),
            )

        async with AsyncWitnessClient(
            config, hotkey=key, openai_api_key="x" * 80, delivery_timeout=5
        ) as client:
            monkeypatch.setattr(
                "witness_client.async_client.time",
                SimpleNamespace(
                    monotonic=lambda: clock[0],
                    time=lambda: clock[0],
                ),
            )
            monkeypatch.setattr(
                "witness_client.async_client.asyncio",
                SimpleNamespace(
                    sleep=sleep,
                    Lock=asyncio.Lock,
                    to_thread=asyncio.to_thread,
                ),
            )
            monkeypatch.setattr(client._gateway, "start", start)
            monkeypatch.setattr(client._gateway, "recorded", recorded)
            if recovered:
                assert (await client.create(model="fixture", input="image")).id == "resp_recovered"
                assert starts == [None, first]
            else:
                with pytest.raises(openai.APIConnectionError) as error:
                    await client.create(model="fixture", input="image")
                assert isinstance(error.value.__cause__, WitnessError)
                assert error.value.__cause__.code == "capture_failed"
                assert starts == [None, first, first]
            assert clock[0] < 1005

    asyncio.run(run())


@pytest.mark.parametrize("preparations, call_limit", [(1, 4), (3, 4), (3, 2)])
def test_async_capture_releases_preparation_for_inference_and_recovers_after_cancel(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    preparations: int,
    call_limit: int,
) -> None:
    from collections.abc import Awaitable, Callable

    async def run() -> None:
        key = Keypair.create_from_uri("//Alice")
        prepared, generating, authorize_now, complete_now = (asyncio.Event() for _ in range(4))
        preparing = inference = maximum = exchanges = active = maximum_active = (
            maximum_inference
        ) = 0
        closed: list[str] = []
        async with AsyncWitnessClient(
            config,
            hotkey=key,
            openai_api_key="x" * 80,
            parallel_calls=call_limit,
            parallel_preparations=preparations,
        ) as client:
            _, body = client._request(0, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body, phase="assigned")
            await service.connect(client)

            class Socket:
                async def send(self, message: str | bytes) -> None:
                    pass

                async def recv(self) -> str:
                    return '{"type":"status","status":"running","handshook":true,"authorized":true}'

                async def close(self) -> None:
                    pass

            @asynccontextmanager
            async def tunnel(
                identifier: str, *, deadline: float
            ) -> AsyncIterator[AsyncWebSocketTunnel]:
                try:
                    yield AsyncWebSocketTunnel(Socket(), identifier)
                finally:
                    closed.append(identifier)

            async def exchange(
                tunnel: AsyncWebSocketTunnel, *args: object, **kwargs: object
            ) -> bytes:
                nonlocal \
                    preparing, \
                    inference, \
                    maximum, \
                    exchanges, \
                    active, \
                    maximum_active, \
                    maximum_inference
                exchanges += 1
                active += 1
                maximum_active = max(maximum_active, active)
                preparing += 1
                maximum = max(maximum, preparing)
                if preparing == min(preparations, call_limit):
                    prepared.set()
                await authorize_now.wait()
                disclosure = cast(
                    EarlyDisclosure,
                    {
                        "private": [],
                        "disclosures": {"client": [], "server": []},
                        "response_key": {},
                        "client_bytes": 1,
                        "probe": True,
                    },
                )
                for phase in ("handshake", "authorize"):
                    await cast(Callable[[EarlyDisclosure], Awaitable[None]], kwargs[phase])(
                        disclosure
                    )
                preparing -= 1
                inference += 1
                maximum_inference = max(maximum_inference, inference)
                service.calls[tunnel.identifier].call.authorized = True
                service.calls[tunnel.identifier].call.phase = "capturing"
                if inference == call_limit:
                    generating.set()
                try:
                    await complete_now.wait()
                finally:
                    inference -= 1
                    active -= 1
                service.complete(tunnel.identifier)
                return (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(service.response)}\r\n\r\n".encode()
                    + service.response
                )

            monkeypatch.setattr(client._gateway, "tunnel", tunnel)
            monkeypatch.setattr("witness_client.async_client.async_exchange", exchange)
            tasks = [
                asyncio.create_task(client.create(model="fixture", input="image", request_id=i))
                for i in range(4)
            ]
            try:
                await asyncio.wait_for(prepared.wait(), 2)
                await asyncio.sleep(0)
                assert preparing == maximum == min(preparations, call_limit)
                authorize_now.set()
                await asyncio.wait_for(generating.wait(), 2)
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                cancelled_id = next(
                    identifier
                    for identifier, snapshot in service.calls.items()
                    if snapshot.call.request.request_id == 0
                )
                service.complete(cancelled_id)
                complete_now.set()
                assert all(
                    response.id == "resp_fixture" for response in await asyncio.gather(*tasks[1:])
                )
                assert (
                    await client.create(model="fixture", input="image", request_id=0)
                ).id == "resp_fixture"
                assert maximum == min(preparations, call_limit) and exchanges == len(closed) == 4
                assert maximum_active == maximum_inference == call_limit
            finally:
                authorize_now.set()
                complete_now.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_cancelled_native_initialization_closes_client_resources(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released, finished = Event(), Event()

    async def run() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        class SlowProver:
            def __init__(self, parameters: Path) -> None:
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert released.wait(3)
                finally:
                    finished.set()

        monkeypatch.setattr("witness_client.async_client.Prover", SlowProver)
        client = AsyncWitnessClient(
            config, hotkey=Keypair.create_from_uri("//Alice"), openai_api_key="x" * 80
        )
        task = asyncio.create_task(client.__aenter__())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client._http.is_closed and client._gateway._artifacts.http.is_closed
        finally:
            released.set()
            assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(run())


@pytest.mark.parametrize("waiting_for", ["capture", "preparation"])
def test_async_queued_cancellation_failure_and_saved_recovery_do_not_leak_capacity(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    waiting_for: str,
) -> None:
    from collections.abc import Awaitable, Callable

    async def run() -> None:
        entered = asyncio.Event()
        opened: list[int] = []
        key = Keypair.create_from_uri("//Alice")
        async with AsyncWitnessClient(
            config,
            hotkey=key,
            openai_api_key="x" * 80,
            parallel_calls=1 if waiting_for == "capture" else 2,
            parallel_preparations=1,
        ) as client:
            _, body = client._request(2, None, {"model": "fixture", "input": "image"})
            service = PodService(key, body)
            await service.connect(client)
            assert (
                await client.create(model="fixture", input="image", request_id=2)
            ).id == "resp_fixture"
            service.phase = "assigned"

            class Socket:
                async def send(self, message: str | bytes) -> None:
                    pass

                async def recv(self) -> str:
                    return '{"type":"status","status":"running","authorized":true}'

                async def close(self) -> None:
                    pass

            @asynccontextmanager
            async def tunnel(
                identifier: str, *, deadline: float
            ) -> AsyncIterator[AsyncWebSocketTunnel]:
                yield AsyncWebSocketTunnel(Socket(), identifier)

            async def exchange(
                tunnel: AsyncWebSocketTunnel, *args: object, **kwargs: object
            ) -> bytes:
                invocation = service.calls[tunnel.identifier].call.request.request_id
                opened.append(invocation)
                if invocation == 0:
                    entered.set()
                    await asyncio.Event().wait()
                if invocation == 3:
                    raise RuntimeError("Fixture proof preparation failed")
                disclosure = cast(
                    EarlyDisclosure,
                    {
                        "private": [],
                        "disclosures": {"client": [], "server": []},
                        "response_key": {},
                        "client_bytes": 1,
                        "probe": True,
                    },
                )
                await cast(Callable[[EarlyDisclosure], Awaitable[None]], kwargs["authorize"])(
                    disclosure
                )
                service.complete(tunnel.identifier)
                return (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(service.response)}\r\n\r\n".encode()
                    + service.response
                )

            monkeypatch.setattr(client._gateway, "tunnel", tunnel)
            monkeypatch.setattr("witness_client.async_client.async_exchange", exchange)
            active = asyncio.create_task(
                client.create(model="fixture", input="image", request_id=0)
            )
            await asyncio.wait_for(entered.wait(), 2)
            queued = asyncio.create_task(
                client.create(model="fixture", input="image", request_id=1)
            )
            try:
                async with asyncio.timeout(2):
                    while len(service.starts) < 3:
                        await asyncio.sleep(0)
                assert not queued.done() and opened == [0]
                # Retained responses must bypass both saturated capacities.
                assert (
                    await asyncio.wait_for(
                        client.create(model="fixture", input="image", request_id=2), 2
                    )
                ).id == "resp_fixture"
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                active.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await active
                with pytest.raises(openai.APIConnectionError):
                    await asyncio.wait_for(
                        client.create(model="fixture", input="image", request_id=3), 2
                    )
                assert (
                    await asyncio.wait_for(
                        client.create(model="fixture", input="image", request_id=4), 2
                    )
                ).id == "resp_fixture"
                assert opened == [0, 3, 4]
            finally:
                for task in (active, queued):
                    task.cancel()
                await asyncio.gather(active, queued, return_exceptions=True)

    asyncio.run(run())
