"""Intermediate renders through the authenticated Pod; no live renderer or R2."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from support import MemoryState
from test_artifacts import IMAGE, PNG, SOURCE, client, enable
from test_service import Harness

from pod_gateway.models import Scope
from pod_gateway.renderer import MAX_SUCCESSFUL_RENDERS, RenderCache


async def enable_cache(fixture: Harness, *, queries: list[str] | None = None) -> list[str]:
    calls: list[str] = []

    async def backend(request: web.Request) -> web.Response:
        source = (await request.json())["source"]
        calls.append(source)
        if queries is not None:
            queries.append(request.query_string)
        if source == "invalid":
            return web.json_response({"error": "validation_failed", "failures": []}, status=422)
        if source == "unavailable":
            return web.json_response({"error": "renderer_unavailable"}, status=503)
        if source == "bad-png":
            return web.Response(body=b"invalid PNG", content_type="image/png")
        if source == "bad-rejection":
            return web.Response(body=b"invalid rejection", content_type="application/json", status=422)
        return web.Response(body=PNG, content_type="image/png")

    app = web.Application()
    app.router.add_post("/render/grid", backend)
    origin = await fixture.listen(app, None)
    service = fixture.services[-1]
    service.renderer_url = origin + "/render/grid"
    service.render_cache = RenderCache(fixture.state, fixture.scope, IMAGE)
    return calls


def fill(cache: RenderCache, count: int) -> None:
    for index in range(count):
        assert cache.save(cache.key(f"saved-{index}".encode(), "/v1/render/grid"), PNG, 200)


def test_identical_previews_reuse_the_saved_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable_cache(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    results = await asyncio.gather(*(asyncio.to_thread(renderer.render, SOURCE) for _ in range(3)))
                    assert results == [PNG] * 3
                    assert await asyncio.to_thread(renderer.render, SOURCE) == PNG
                assert rendered == [SOURCE]

    asyncio.run(scenario())


@pytest.mark.parametrize("query", ["img_size=64.0", "gap=0.0", "cam_radius=-2"])
def test_renderer_queries_pass_through_and_reuse_the_saved_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                queries: list[str] = []
                calls = await enable_cache(fixture, queries=queries)
                with httpx.Client(transport=witness.renderer_transport()) as http:
                    url = "https://witness.internal/v1/render/grid?" + query
                    for _ in range(2):
                        reply = await asyncio.to_thread(http.post, url, json={"source": SOURCE})
                        assert reply.status_code == 200 and reply.content == PNG
                assert queries == [query] and calls == [SOURCE]

    asyncio.run(scenario())


def test_render_source_and_query_are_part_of_the_cache_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                calls = await enable_cache(fixture)
                with httpx.Client(transport=witness.renderer_transport()) as http:
                    url = "https://witness.internal/v1/render/grid"
                    for source, params in (
                        (SOURCE, {}), (SOURCE, {}), (SOURCE + "\n", {}),
                        (SOURCE, {"lighting": "neutral"}), (SOURCE, {"lighting": "neutral"}),
                    ):
                        reply = await asyncio.to_thread(http.post, url, params=params, json={"source": source})
                        assert reply.content == PNG
                assert calls == [SOURCE, SOURCE + "\n", SOURCE]

    asyncio.run(scenario())


def test_only_new_saved_pngs_consume_the_allowance_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                calls = await enable_cache(fixture)
                cache = fixture.services[-1].render_cache
                assert cache is not None
                fill(cache, MAX_SUCCESSFUL_RENDERS - 1)
                # Completed finals and other rounds do not consume this allowance.
                fixture.state.put(f"rounds/{fixture.scope.round}/{fixture.scope.hotkey}/submitted/grid.png", PNG)
                fixture.state.put(f"rounds/another-round/{fixture.scope.hotkey}/renders/v1/other.png", PNG)
                fixture.services[-1].render_cache = RenderCache(fixture.state, fixture.scope, IMAGE)
                with httpx.Client(transport=witness.renderer_transport()) as http:
                    url = "https://witness.internal/v1/render/grid"
                    for source, status in (
                        ("unavailable", 503), ("unavailable", 503), ("invalid", 422), ("invalid", 422),
                        ("bad-png", 503), ("bad-rejection", 503),
                        (SOURCE, 200), (SOURCE, 200), ("one-too-many", 429), ("invalid", 422),
                    ):
                        reply = await asyncio.to_thread(http.post, url, json={"source": source})
                        assert reply.status_code == status, reply.text
                    fixture.services[-1].render_cache = RenderCache(fixture.state, fixture.scope, IMAGE)
                    assert (await asyncio.to_thread(http.post, url, json={"source": SOURCE})).content == PNG
                    rejected = await asyncio.to_thread(http.post, url, json={"source": "one-too-many"})
                    assert rejected.status_code == 429 and rejected.json() == {"error": "render_limit_exhausted"}
                assert calls == ["unavailable", "unavailable", "invalid", "bad-png", "bad-rejection", SOURCE]

    asyncio.run(scenario())


def test_failed_upload_is_retryable_and_does_not_spend_the_last_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                calls = await enable_cache(fixture)
                cache = fixture.services[-1].render_cache
                assert cache is not None
                fill(cache, MAX_SUCCESSFUL_RENDERS - 1)
                put = fixture.state.put
                failed = False

                def upload(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
                    nonlocal failed
                    if key.endswith(".png") and not failed:
                        failed = True
                        raise OSError("R2 unavailable")
                    put(key, data, content_type)

                monkeypatch.setattr(fixture.state, "put", upload)
                with httpx.Client(transport=witness.renderer_transport()) as http:
                    url = "https://witness.internal/v1/render/grid"
                    first = await asyncio.to_thread(http.post, url, json={"source": SOURCE})
                    assert first.status_code == 503
                    assert (await asyncio.to_thread(http.post, url, json={"source": SOURCE})).content == PNG
                    assert (await asyncio.to_thread(http.post, url, json={"source": "new"})).status_code == 429
                assert calls == [SOURCE, SOURCE]

    asyncio.run(scenario())


def test_saved_renders_are_scoped_to_the_round_miner_and_renderer() -> None:
    objects = MemoryState()
    scope = Scope("test", "round-0", "5" * 48)
    cache = RenderCache(objects, scope, IMAGE, storage_prefix="experiments/run/")
    key = cache.key(SOURCE.encode(), "/v1/render/grid")
    assert cache.save(key, PNG, 200)
    restarted = RenderCache(objects, scope, IMAGE, storage_prefix="experiments/run/")
    assert restarted.get(key) == (PNG, 200)
    for changed_scope, renderer_id in (
        (replace(scope, round="round-1"), IMAGE), (replace(scope, hotkey="6" * 48), IMAGE),
        (scope, "sha256:" + "b" * 64),
    ):
        changed = RenderCache(objects, changed_scope, renderer_id, storage_prefix="experiments/run/")
        assert changed.get(changed.key(SOURCE.encode(), "/v1/render/grid")) is None


def test_final_saving_does_not_consume_the_preview_allowance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                cache = RenderCache(fixture.state, fixture.scope, IMAGE)
                fill(cache, MAX_SUCCESSFUL_RENDERS)
                fixture.services[-1].render_cache = cache
                with client(fixture, witness.renderer_transport()) as renderer:
                    saved = await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert saved["failure_reason"] is None

    asyncio.run(scenario())
