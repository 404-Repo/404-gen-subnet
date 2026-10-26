from unittest.mock import AsyncMock, patch

import httpx
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient, MockR2Client

from submission_collector.download import DownloadPipeline
from submission_collector.settings import Settings


def _make_pipeline(git: MockGitHubClient, r2: MockR2Client, settings: Settings) -> DownloadPipeline:
    git_batcher = GitBatcher(git=git, base_sha="abc123", branch=settings.github_branch)
    return DownloadPipeline(
        git_batcher=git_batcher,
        r2=r2,
        http_client=httpx.AsyncClient(),
        settings=settings,
    )


async def test_fetch_prompt_images_drops_failed_urls(git: MockGitHubClient, settings: Settings) -> None:
    """One URL fails, others succeed: failed stems drop out, successful ones come through."""
    pipeline = _make_pipeline(git, MockR2Client(), settings)

    async def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        if "chair" in url:
            return httpx.Response(200, content=b"chair-bytes", request=request)
        return httpx.Response(404, request=request)

    with patch.object(pipeline.http_client, "get", new=AsyncMock(side_effect=fake_get)):
        result = await pipeline._fetch_prompt_images(["https://example.com/chair.png", "https://example.com/table.png"])

    assert result == {"chair": b"chair-bytes"}


async def test_fetch_prompt_images_all_failures_returns_empty(git: MockGitHubClient, settings: Settings) -> None:
    """Every URL fails: empty dict (not exception, not None entries)."""
    pipeline = _make_pipeline(git, MockR2Client(), settings)

    async def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("GET", url))

    with patch.object(pipeline.http_client, "get", new=AsyncMock(side_effect=fake_get)):
        result = await pipeline._fetch_prompt_images(["https://example.com/a.png", "https://example.com/b.png"])

    assert result == {}
