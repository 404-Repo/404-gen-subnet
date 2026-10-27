"""Preview rendering through the witness client's existing Pod connection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from bittensor_wallet import Keypair
from test_client_api import config as config
from test_submission import SOURCE, Mode, connected
from test_submission import isolated_submission as isolated_submission
from witness_client import (
    AsyncWitnessClient,
    RenderOptions,
    RenderRejected,
    RenderUnavailable,
    WitnessClient,
    WitnessConfig,
)

PNG = b"\x89PNG\r\n\x1a\nfixture"
GRID = RenderOptions(lighting="neutral")


async def render(
    client: WitnessClient | AsyncWitnessClient, source: str, options: RenderOptions = GRID
) -> bytes:
    if isinstance(client, AsyncWitnessClient):
        return await client.render(source, options)
    return client.render(source, options)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_render_reuses_the_authenticated_pod_and_preserves_options_and_timeout(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
) -> None:
    calls: list[httpx.Request] = []
    options = RenderOptions(thetas=(30,), phis=(-10,), img_size=256, lighting="neutral")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url).split("?", 1)[0] == "https://192.0.2.1:8443/v1/render/grid"
        assert dict(request.url.params) == options.query()
        assert json.loads(request.content) == {"source": SOURCE}
        assert request.headers["authorization"] == "Bearer scoped-access"
        assert request.extensions["sni_hostname"] == "witness.internal"
        assert request.extensions["timeout"] == {
            name: 240 for name in ("connect", "read", "write", "pool")
        }
        key = Keypair.create_from_uri("//Alice")
        payload = "\n".join(
            [
                "btauth/1",
                "sr25519",
                "POST",
                request.url.raw_path.decode("ascii"),
                hashlib.sha256(request.content).hexdigest(),
                request.headers["x-bittensor-nonce"],
                key.ss58_address,
                "404-witness",
            ]
        )
        assert key.verify(
            payload.encode(), bytes.fromhex(request.headers["x-bittensor-signature"][2:])
        )
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    async def run() -> None:
        async with connected(config, mode, handler) as client:
            direct = client._gateway._direct
            assert direct is not None
            owned = direct.http
            with monkeypatch.context() as patch:

                def forbidden(*args: object, **kwargs: object) -> None:
                    pytest.fail("render() must reuse the connected client")

                patch.setattr(httpx, "Client", forbidden)
                patch.setattr(httpx, "AsyncClient", forbidden)
                assert await render(client, SOURCE, options) == PNG
            assert client._gateway._direct is direct and not owned.is_closed
        assert owned.is_closed and len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_render_preserves_structured_rejections(config: WitnessConfig, mode: Mode) -> None:
    details = {
        "error": "validation_failed",
        "failures": [{"code": "geometry"}],
        "metrics": {"faces": 0},
    }

    async def run() -> None:
        async with connected(config, mode, lambda _: httpx.Response(422, json=details)) as client:
            with pytest.raises(RenderRejected) as rejected:
                await render(client, SOURCE)
            assert rejected.value.details == details

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "status,body,content_type",
    [
        (429, b'{"error":"render_busy"}', "application/json"),
        (503, b'{"error":"unavailable"}', "application/json"),
        (422, b"malformed rejection", "text/plain"),
        (200, PNG, "text/plain"),
        (200, b"invalid PNG", "image/png"),
        (200, PNG + b"x" * (32 * 1024 * 1024), "image/png"),
    ],
)
def test_render_propagates_errors_without_retry(
    config: WitnessConfig,
    mode: Mode,
    status: int,
    body: bytes,
    content_type: str,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    async def run() -> None:
        async with connected(config, mode, handler) as client:
            with pytest.raises(RenderUnavailable):
                await render(client, SOURCE)
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_render_propagates_transport_failure_without_retry(
    config: WitnessConfig, mode: Mode
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("Preview reply lost", request=request)

    async def run() -> None:
        async with connected(config, mode, handler) as client:
            with pytest.raises(RenderUnavailable) as failed:
                await render(client, SOURCE)
            assert isinstance(failed.value.__cause__, httpx.ReadError)
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_render_checks_source_size_before_network(config: WitnessConfig, mode: Mode) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Oversized source must not reach the Pod")

    async def run() -> None:
        async with connected(config, mode, forbidden) as client:
            with pytest.raises(ValueError, match="1 MiB"):
                await render(client, "é" * (512 * 1024 + 1))

    asyncio.run(run())


def test_cancelled_async_render_closes_response_and_leaves_the_client_usable(
    config: WitnessConfig,
) -> None:
    async def run() -> None:
        entered, released = asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                entered.set()
                await asyncio.Event().wait()
                yield PNG

            async def aclose(self) -> None:
                released.set()

        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, headers={"content-type": "image/png"}, stream=Stream())
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

        async with connected(config, "async", handler) as client:
            task = asyncio.create_task(render(client, SOURCE))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert released.is_set()
            assert await render(client, SOURCE) == PNG
        assert calls == 2

    asyncio.run(run())
