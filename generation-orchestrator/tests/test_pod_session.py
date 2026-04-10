from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from subnet_common.testing.mock_r2 import MockR2Client

from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.pod_client import BatchResults, PodStatus, PodStatusResponse
from generation_orchestrator.pod_session import BatchComplete, PodReplaceRequested, PodSession, ReplaceReason
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.settings import Settings


FIXTURES_DIR = Path(__file__).parent / "fixtures"
HOTKEY = "5abc123def"


def _render_views_ok() -> AsyncMock:
    """AsyncMock for `render_views` that returns {view.name: b"png"} matching the requested views.

    Accepts either positional or keyword `views` since pod_session passes it positionally.
    """

    async def _fake(*args: Any, **kwargs: Any) -> dict[str, bytes]:
        # Signature: render_views(client, endpoint, js_content, views, bg_color, log_id, *, api_key=...)
        views = kwargs["views"] if "views" in kwargs else args[3]
        return {v.name: b"png" for v in views}

    return AsyncMock(side_effect=_fake)


_FAKE_EMBEDS_SHAPE = (1 + 8, 384)  # 1 prompt + 8 white views


def _render_grid_ok() -> AsyncMock:
    return AsyncMock(return_value=b"grid-png")


def _embeddings_ok() -> AsyncMock:
    import numpy as np

    return AsyncMock(return_value=np.zeros(_FAKE_EMBEDS_SHAPE, dtype=np.float32))


def make_session(
    settings: Settings,
    stop: GenerationStop | None = None,
) -> PodSession:
    return PodSession(
        settings=settings,
        pod_endpoint="http://fake-pod:10006",
        auth_token=None,
        hotkey=HOTKEY,
        seed=42,
        stop=stop or GenerationStop(),
        remaining_replacements=3,
    )


def make_prompt(stem: str) -> Prompt:
    return Prompt(stem=stem, url=f"https://cdn/{stem}.jpg", path=FIXTURES_DIR / "prompt1.jpg")


async def test_run_success(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1"), make_prompt("p2")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.COMPLETE)),
        ),
        patch(
            "generation_orchestrator.pod_session.download_batch",
            AsyncMock(return_value=BatchResults(successes={"p1": b"js1", "p2": b"js2"})),
        ),
        patch("generation_orchestrator.pod_session.render_views", _render_views_ok()),
        patch("generation_orchestrator.pod_session.render_grid", _render_grid_ok()),
        patch("generation_orchestrator.pod_session.calculate_embeddings", _embeddings_ok()),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, BatchComplete)
    assert set(result.generations) == {"p1", "p2"}
    assert result.generations["p1"].js is not None
    assert result.generations["p1"].views is not None
    assert result.generations["p1"].failure_reason is None
    assert result.batch_time > 0


async def test_run_partial_batch_records_failures(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1"), make_prompt("p2"), make_prompt("p3")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.COMPLETE)),
        ),
        patch(
            "generation_orchestrator.pod_session.download_batch",
            AsyncMock(
                return_value=BatchResults(
                    successes={"p1": b"js1"},
                    failures={"p2": "inference timeout"},
                )
            ),
        ),
        patch("generation_orchestrator.pod_session.render_views", _render_views_ok()),
        patch("generation_orchestrator.pod_session.render_grid", _render_grid_ok()),
        patch("generation_orchestrator.pod_session.calculate_embeddings", _embeddings_ok()),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, BatchComplete)
    assert result.generations["p1"].js is not None
    assert result.generations["p2"].failure_reason == "inference timeout"
    # p3 was in neither — spec says treat as miner failure.
    assert result.generations["p3"].failure_reason is not None


async def test_run_render_failure_persists_js_without_views(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.COMPLETE)),
        ),
        patch(
            "generation_orchestrator.pod_session.download_batch",
            AsyncMock(return_value=BatchResults(successes={"p1": b"js1"})),
        ),
        patch("generation_orchestrator.pod_session.render_views", AsyncMock(return_value=None)),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, BatchComplete)
    assert result.generations["p1"].js is not None
    assert result.generations["p1"].views is None
    assert result.generations["p1"].failure_reason is None


async def test_run_submission_failure(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=False)),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, PodReplaceRequested)
    assert result.reason == ReplaceReason.SUBMISSION_FAILED


async def test_run_replace_mid_batch_returns_payload(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1")]

    payload = {"gpus": [{"id": 0, "passed": False}]}
    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.REPLACE, payload=payload)),
        ),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, PodReplaceRequested)
    assert result.reason == ReplaceReason.REQUESTED
    assert result.payload == payload


async def test_run_download_failure(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.COMPLETE)),
        ),
        patch("generation_orchestrator.pod_session.download_batch", AsyncMock(return_value=None)),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, PodReplaceRequested)
    assert result.reason == ReplaceReason.DOWNLOAD_FAILED


async def test_run_batch_time_limit_exceeded(settings: Settings) -> None:
    """When `batch_time_limit_seconds` elapses during polling, return BATCH_TIME_LIMIT."""
    settings.status_check_interval_seconds = 0
    settings.batch_time_limit_seconds = 0  # fire on first elapsed check (elapsed > 0)
    batch = [make_prompt("p1")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.GENERATING, progress=1, total=1)),
        ),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, PodReplaceRequested)
    assert result.reason == ReplaceReason.BATCH_TIME_LIMIT


async def test_run_pod_unreachable_mid_batch(settings: Settings) -> None:
    settings.status_check_interval_seconds = 0
    settings.max_consecutive_unhealthy = 2
    batch = [make_prompt("p1")]

    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch("generation_orchestrator.pod_session.check_pod_status", AsyncMock(return_value=None)),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, PodReplaceRequested)
    assert result.reason == ReplaceReason.UNREACHABLE


async def test_payload_too_large_is_dropped(settings: Settings) -> None:
    """Oversize REPLACE payloads are logged and dropped before returning to the caller."""
    settings.status_check_interval_seconds = 0
    batch = [make_prompt("p1")]

    big_payload = {"data": "x" * 10000}
    with (
        patch("generation_orchestrator.pod_session.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.pod_session.httpx.AsyncClient") as MockHttpx,
        patch("generation_orchestrator.pod_session.submit_batch", AsyncMock(return_value=True)),
        patch(
            "generation_orchestrator.pod_session.check_pod_status",
            AsyncMock(return_value=PodStatusResponse(status=PodStatus.REPLACE, payload=big_payload)),
        ),
    ):
        MockHttpx.return_value.aclose = AsyncMock()
        async with make_session(settings) as session:
            result = await session.run(round_num=1, batch=batch)

    assert isinstance(result, PodReplaceRequested)
    assert result.reason == ReplaceReason.REQUESTED
    assert result.payload is None
