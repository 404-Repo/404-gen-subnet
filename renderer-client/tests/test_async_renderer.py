"""Awaitable renderer calls preserve options, errors, bounds and cancellation."""

import asyncio
import json

import httpx
import pytest
from test_renderer import DETAILS, IMAGE, PNG, SOURCE

from renderer_client import AsyncRendererClient, RenderOptions, RenderRejected, RenderUnavailable


def test_async_renders_overlap_and_preserve_every_option():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        options = RenderOptions(
            thetas=(30, 120),
            phis=(0, -30),
            img_size=1024,
            cam_radius=3,
            cam_fov_deg=40,
            gap=0,
            bg_color="FFFFFF",
            lighting="neutral",
        )

        async def handler(request):
            calls.append(request)
            assert request.url.path == "/v1/render/grid"
            assert dict(request.url.params) == options.query()
            assert request.headers["authorization"] == "Bearer fixture-key"
            assert json.loads(request.content) == {"source": SOURCE}
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

        async with AsyncRendererClient(
            "https://renderer.test/v1",
            renderer_id=IMAGE,
            api_key="fixture-key",
            transport=httpx.MockTransport(handler),
        ) as renderer:
            tasks = [asyncio.create_task(renderer.render(SOURCE, options)) for _ in range(2)]
            try:
                await asyncio.wait_for(entered.wait(), 1)
            finally:
                release.set()
            assert await asyncio.gather(*tasks) == [PNG, PNG]
        assert renderer._http.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 503])
def test_async_infrastructure_errors_do_not_retry_or_become_rejections(status):
    async def run():
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(status, headers={"location": "https://other.test"})

        async with AsyncRendererClient(
            "https://renderer.test",
            renderer_id=IMAGE,
            transport=httpx.MockTransport(handler),
        ) as renderer:
            with pytest.raises(RenderUnavailable):
                await renderer.render(SOURCE)
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(422, content=b"not JSON"),
        httpx.Response(422, json=[]),
        httpx.Response(200, headers={"content-type": "text/html"}, content=PNG),
        httpx.Response(200, headers={"content-type": "image/png"}, content=b"not PNG"),
    ],
)
def test_async_malformed_responses_are_unavailable(response):
    async def run():
        async with AsyncRendererClient(
            "https://renderer.test",
            renderer_id=IMAGE,
            transport=httpx.MockTransport(lambda _: response),
        ) as renderer:
            with pytest.raises(RenderUnavailable):
                await renderer.render(SOURCE)

    asyncio.run(run())


def test_async_structured_rejection_preserves_details():
    async def run():
        async with AsyncRendererClient(
            "https://renderer.test",
            renderer_id=IMAGE,
            transport=httpx.MockTransport(lambda _: httpx.Response(422, json=DETAILS)),
        ) as renderer:
            with pytest.raises(RenderRejected) as rejected:
                await renderer.render(SOURCE)
            assert rejected.value.details == DETAILS

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_async_stream_is_closed_on_size_limit_and_cancellation(monkeypatch, cancel):
    from renderer_client import async_client as module

    async def run():
        reading = asyncio.Event()
        closed = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                if cancel:
                    reading.set()
                    await asyncio.Event().wait()
                yield PNG * 1000

            async def aclose(self):
                closed.append(True)

        monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(PNG))
        async with AsyncRendererClient(
            "https://renderer.test",
            renderer_id=IMAGE,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "image/png"}, stream=Stream())
            ),
        ) as renderer:
            task = asyncio.create_task(renderer.render(SOURCE))
            if cancel:
                await asyncio.wait_for(reading.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RenderUnavailable, match="size limit"):
                    await task
            assert closed == [True]
        assert renderer._http.is_closed

    asyncio.run(run())
