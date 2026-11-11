"""Final submission reuses the live Pod connection and never retries inference."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from bittensor_wallet import Keypair
from test_async_transport import tls_context as tls_context
from test_config import config as config
from witness_client import AsyncWitnessClient, WitnessConfig, WitnessError
from witness_client.async_pods import AsyncSignedTransport

SOURCE = "export default function generate(THREE) { return new THREE.Group(); }"
IMAGE = b"frozen prompt bytes"
RESULT: dict[str, object] = {
    "js": "submitted.js",
    "views": "submitted",
    "failure_reason": None,
    "size": len(SOURCE),
}


@pytest.fixture(autouse=True)
def isolated_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    class ProverStub:
        def __init__(self, parameters: Path) -> None:
            pass

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Submission must not contact a real service")

    monkeypatch.setattr("witness_client.async_client.Prover", ProverStub)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@asynccontextmanager
async def connected(
    config: WitnessConfig,
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[AsyncWitnessClient]:
    key = Keypair.create_from_uri("//Alice")
    live: AsyncWitnessClient
    live = AsyncWitnessClient(config, hotkey=key, openai_api_key="x" * 80)
    async_http = httpx.AsyncClient(
        base_url="https://192.0.2.1:8443", transport=httpx.MockTransport(handler)
    )
    live._gateway._direct = AsyncSignedTransport(
        async_http,
        key,
        5,
        headers={"Authorization": "Bearer scoped-access"},
        server_name="witness.internal",
    )
    live._gateway._connections.append(async_http)
    live._gateway._start_id = str(uuid.uuid4())
    live._gateway.round_number = "configured-round"
    try:
        yield live
    finally:
        await live.aclose()


async def submit(
    client: AsyncWitnessClient, image: bytes, source: str | None
) -> dict[str, object]:
    return await client.submit(image, source)


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.ReadTimeout],
)
@pytest.mark.parametrize("replaced", [False, True])
def test_transport_failure_recovers_current_pod_without_forcing_replacement(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    tls_context: tuple[object, Path],
    raw: bool,
    failure: type[httpx.TransportError],
    replaced: bool,
) -> None:
    failed = False
    old_start = str(uuid.uuid4())
    current_start = str(uuid.uuid4()) if replaced else old_start
    allocation_calls: list[tuple[str, str]] = []
    pod_hosts: list[str] = []
    reconnect = failure in (httpx.ConnectError, httpx.ConnectTimeout)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        if request.url.host == "witness.test":
            allocation_calls.append((request.method, request.url.path))
            return httpx.Response(200, json={
                "round": "configured-round", "retry_after": 0,
                "session": {"phase": "allocated", "start_id": current_start, "pod_id": "current"},
                "witness": {"start_id": current_start, "pod_id": "current",
                            "address": "192.0.2.2" if replaced else "192.0.2.1", "port": 8443,
                            "server_name": "witness.internal", "certificate": tls_context[1].read_text()},
            })
        pod_hosts.append(str(request.url.host))
        if not failed:
            failed = True
            raise failure("Pod connection failed", request=request)
        return httpx.Response(200, json={"submissions": {}})

    sync_type, async_type = httpx.Client, httpx.AsyncClient

    def sync_http(*args: Any, **kwargs: Any) -> httpx.Client:
        return sync_type(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})

    def async_http(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return async_type(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(httpx, "Client", sync_http)
    monkeypatch.setattr(httpx, "AsyncClient", async_http)

    async def run() -> None:
        async with connected(config, handler) as live:
            live._gateway._start_id = old_start

            async def lookup() -> dict[str, object]:
                if raw:
                    data = await live._gateway._request_once("GET", "/v1/submissions")
                    return {"raw": json.loads(data or b"{}")}
                return {"submitted": await get_submitted(live)}

            with pytest.raises((httpx.TransportError, WitnessError)):
                await lookup()
            expected: dict[str, object] = {"raw": {"submissions": {}}} if raw else {"submitted": {}}
            assert await lookup() == expected
            assert await lookup() == expected
        assert allocation_calls == ([("GET", "/v1/pods/current")] if reconnect else [])
        current_host = "192.0.2.2" if replaced and reconnect else "192.0.2.1"
        assert pod_hosts == ["192.0.2.1", current_host, current_host]

    asyncio.run(run())


@pytest.mark.parametrize("source", [SOURCE, "", None])
def test_submit_computes_prompt_identity_and_reuses_owned_authenticated_connection(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    source: str | None,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST" and request.url == "https://192.0.2.1:8443/v1/submissions"
        assert request.headers["authorization"] == "Bearer scoped-access"
        assert request.extensions["sni_hostname"] == "witness.internal"
        assert request.extensions["timeout"] == {
            "connect": 150, "read": None, "write": 150, "pool": 150,
        }
        assert json.loads(request.content) == {
            "generation": {"prompt_sha256": hashlib.sha256(IMAGE).hexdigest()},
            "source": source,
            "prompt_image": base64.b64encode(IMAGE).decode() if source is not None else None,
        }
        key = Keypair.create_from_uri("//Alice")
        payload = "\n".join(
            [
                "btauth/1",
                "sr25519",
                "POST",
                "/v1/submissions",
                hashlib.sha256(request.content).hexdigest(),
                request.headers["x-bittensor-nonce"],
                key.ss58_address,
                "404-witness",
            ]
        )
        assert key.verify(
            payload.encode(), bytes.fromhex(request.headers["x-bittensor-signature"][2:])
        )
        return httpx.Response(200, json={"submission": RESULT})

    async def run() -> None:
        async with connected(config, handler) as live:
            direct = live._gateway._direct
            assert direct is not None
            owned = direct.http
            with monkeypatch.context() as patch:

                def forbidden(*args: object, **kwargs: object) -> None:
                    pytest.fail("submit() must reuse the connected client")

                patch.setattr(httpx, "Client", forbidden)
                patch.setattr(httpx, "AsyncClient", forbidden)
                assert await submit(live, IMAGE, source) == RESULT
            assert live._gateway._direct is direct and not owned.is_closed
        assert owned.is_closed and len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_request"),
        (401, "allocation_access_expired"),
        (403, "round_closed"),
        (409, "RecordingConflict"),
        (422, "validation_failed"),
        (429, "save_in_progress"),
        (503, "saving_unavailable"),
    ],
)
def test_submit_propagates_server_failure_without_retry(
    config: WitnessConfig,
    status: int,
    code: str,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": code})

    async def run() -> None:
        async with connected(config, handler) as live:
            with pytest.raises(WitnessError) as failed:
                await submit(live, IMAGE, SOURCE)
            assert failed.value.status_code == status and failed.value.code == code
            assert failed.value.source == "gateway"
        assert len(calls) == 1

    asyncio.run(run())


def test_submit_propagates_lost_reply_without_repeating_the_request(
    config: WitnessConfig,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("Reply lost", request=request)

    async def run() -> None:
        async with connected(config, handler) as live:
            with pytest.raises(WitnessError) as failed:
                await submit(live, IMAGE, SOURCE)
            assert failed.value.code == "submission_unresolved"
            assert isinstance(failed.value.__cause__, httpx.ReadError)
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "body", [b"not JSON", b"[]", b"{}", b'{"submission":null}', b"x" * (256 * 1024 + 1)]
)
def test_submit_rejects_malformed_saved_metadata(
    config: WitnessConfig, body: bytes
) -> None:
    async def run() -> None:
        async with connected(
            config, lambda _: httpx.Response(200, content=body)
        ) as live:
            with pytest.raises(WitnessError) as failed:
                await submit(live, IMAGE, SOURCE)
            assert failed.value.code == "invalid_submission"

    asyncio.run(run())


@pytest.mark.parametrize(
    "image,source",
    [
        (cast(bytes, "not bytes"), SOURCE),
        (IMAGE, cast(str, 123)),
        (b"x" * (8 * 1024 * 1024 + 1), SOURCE),
        (IMAGE, "я" * (512 * 1024 + 1)),
    ],
)
def test_invalid_submission_fails_before_network(
    config: WitnessConfig, image: bytes, source: str
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input must not reach the Pod")

    async def run() -> None:
        for mode in ("sync", "async"):
            async with connected(config, forbidden) as live:
                with pytest.raises(ValueError):
                    await submit(live, image, source)

    asyncio.run(run())


def test_cancelled_async_submission_closes_response_but_keeps_connection_owned(
    config: WitnessConfig,
) -> None:
    async def run() -> None:
        reading = asyncio.Event()
        closed: list[bool] = []
        calls: list[httpx.Request] = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                reading.set()
                await asyncio.Event().wait()
                yield b"pending"

            async def aclose(self) -> None:
                closed.append(True)

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return (
                httpx.Response(200, stream=Stream())
                if len(calls) == 1
                else httpx.Response(200, json={"submission": RESULT})
            )

        async with connected(config, handler) as live:
            assert isinstance(live, AsyncWitnessClient)
            direct = live._gateway._direct
            assert direct is not None
            task = asyncio.create_task(live.submit(IMAGE, SOURCE))
            await asyncio.wait_for(reading.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed == [True] and not direct.http.is_closed and len(calls) == 1
            assert await live.submit(IMAGE, SOURCE) == RESULT
        assert direct.http.is_closed

    asyncio.run(run())


async def get_submitted(
    client: AsyncWitnessClient,
) -> dict[str, dict[str, object]]:
    return await client.get_submitted()


@pytest.mark.parametrize("saved", [False, True])
def test_get_submitted_returns_saved_outcomes_through_the_owned_connection(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, saved: bool
) -> None:
    entries = {
        "official-chair": RESULT,
        "official-empty": {"js": None, "views": None, "size": 0, "failure_reason": "generation_failed"},
    } if saved else {}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET" and request.url == "https://192.0.2.1:8443/v1/submissions"
        assert not request.content
        assert request.headers["authorization"] == "Bearer scoped-access"
        assert request.extensions["sni_hostname"] == "witness.internal"
        return httpx.Response(200, json={"submissions": entries})

    async def run() -> None:
        async with connected(config, handler) as live:
            direct = live._gateway._direct
            assert direct is not None
            with monkeypatch.context() as patch:
                def forbidden(*args: object, **kwargs: object) -> None:
                    pytest.fail("get_submitted() must reuse the connected client")

                patch.setattr(httpx, "Client", forbidden)
                patch.setattr(httpx, "AsyncClient", forbidden)
                assert await get_submitted(live) == entries
            assert not direct.http.is_closed
        assert direct.http.is_closed and len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "allocation_access_expired"),
        (403, "miner_not_assigned"),
        (404, "artifact_storage_disabled"),
        (503, "witness_unavailable"),
    ],
)
def test_get_submitted_does_not_turn_server_failures_into_empty_results(
    config: WitnessConfig, status: int, code: str
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": code})

    async def run() -> None:
        async with connected(config, handler) as live:
            with pytest.raises(WitnessError) as failed:
                await get_submitted(live)
            assert failed.value.status_code == status and failed.value.code == code
            assert failed.value.source == "gateway"
        assert len(calls) == 1

    asyncio.run(run())


def test_get_submitted_preserves_transport_failure_without_retry(
    config: WitnessConfig,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("Connection lost", request=request)

    async def run() -> None:
        async with connected(config, handler) as live:
            with pytest.raises(WitnessError) as failed:
                await get_submitted(live)
            assert failed.value.code == "submission_lookup_failed"
            assert isinstance(failed.value.__cause__, httpx.ReadError)
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"submissions":null}',
        b'{"submissions":[]}',
        b'{"submissions":{"invalid prompt ID":{}}}',
        json.dumps({"submissions": {"a" * 64: None}}).encode(),
        b"x" * (16 * 1024 * 1024 + 17),
    ],
)
def test_get_submitted_rejects_malformed_metadata(
    config: WitnessConfig, body: bytes
) -> None:
    async def run() -> None:
        async with connected(
            config, lambda _: httpx.Response(200, content=body)
        ) as live:
            with pytest.raises(WitnessError) as failed:
                await get_submitted(live)
            assert failed.value.code == "invalid_submissions"

    asyncio.run(run())
