import json
from unittest.mock import AsyncMock, patch

import httpx
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient, MockR2Client

from submission_collector.download import DownloadPipeline
from submission_collector.settings import Settings


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
REF = "abc123"
GLB_DATA = b"fake-glb-data"
PNG_DATA = b"fake-png-data"


def _setup_round(git: MockGitHubClient, round_num: int, hotkeys: list[str], prompts: list[str]) -> None:
    """Populate git with submissions and prompts for a round."""
    submissions = {
        hk: {
            "repo": "test/model",
            "commit": "a" * 40,
            "cdn_url": f"https://cdn.example.com/{hk[:10]}",
            "revealed_at_block": 150,
            "round": f"test-{round_num}",
        }
        for hk in hotkeys
    }
    git.files[f"rounds/{round_num}/submissions.json"] = json.dumps(submissions)
    git.files[f"rounds/{round_num}/prompts.txt"] = "\n".join(prompts)


def _make_pipeline(git: MockGitHubClient, r2: MockR2Client, settings: Settings) -> DownloadPipeline:
    git_batcher = GitBatcher(git=git, base_sha=REF, branch=settings.github_branch)
    return DownloadPipeline(
        git_batcher=git_batcher,
        r2=r2,
        http_client=httpx.AsyncClient(),
        settings=settings,
    )


@patch("submission_collector.download._fetch_glb", new_callable=AsyncMock, return_value=GLB_DATA)
@patch("submission_collector.download.render", new_callable=AsyncMock, return_value=PNG_DATA)
async def test_happy_path_fetches_renders_uploads(
    mock_render: AsyncMock, mock_fetch: AsyncMock, git: MockGitHubClient, settings: Settings
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair"])
    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)

    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # GLB fetched from miner CDN
    mock_fetch.assert_called_once()
    assert "chair" in str(mock_fetch.call_args)

    # Render called with GLB data
    mock_render.assert_called_once()
    assert mock_render.call_args.kwargs["glb_content"] == GLB_DATA

    # Both GLB and PNG uploaded to R2
    assert len(r2.uploads) == 2
    assert r2.uploads[0]["content_type"] == "application/octet-stream"
    assert r2.uploads[1]["content_type"] == "image/png"

    # Generation result saved to git
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    assert gen_path in git_batcher_committed(git)
    generations = GenerationsAdapter.validate_json(git_batcher_committed(git)[gen_path])
    assert "chair" in generations
    expected_prefix = f"{settings.cdn_url}/rounds/1/{HOTKEY}/submitted/chair"
    assert generations["chair"].glb == f"{expected_prefix}.glb"
    assert generations["chair"].png == f"{expected_prefix}.png"
    assert generations["chair"].size == len(GLB_DATA)


@patch("submission_collector.download._fetch_glb", new_callable=AsyncMock, return_value=GLB_DATA)
@patch("submission_collector.download.render", new_callable=AsyncMock, return_value=PNG_DATA)
async def test_skips_already_completed_prompts(
    mock_render: AsyncMock, mock_fetch: AsyncMock, git: MockGitHubClient, settings: Settings
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair", "table"])

    # "chair" already completed
    existing = {"chair": GenerationResult(glb="https://cdn/chair.glb", png="https://cdn/chair.png", size=100)}
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    git.files[gen_path] = GenerationsAdapter.dump_json(existing).decode()

    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)
    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # Only "table" should be fetched
    mock_fetch.assert_called_once()
    assert "table" in str(mock_fetch.call_args)


@patch("submission_collector.download._fetch_glb", new_callable=AsyncMock, side_effect=Exception("CDN down"))
@patch("submission_collector.download.render", new_callable=AsyncMock, return_value=PNG_DATA)
async def test_fetch_failure_records_empty_generation(
    mock_render: AsyncMock, mock_fetch: AsyncMock, git: MockGitHubClient, settings: Settings
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair"])
    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)

    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # No uploads to R2
    assert len(r2.uploads) == 0

    # Render not called
    mock_render.assert_not_called()

    # Empty generation result saved
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    generations = GenerationsAdapter.validate_json(git_batcher_committed(git)[gen_path])
    assert "chair" in generations
    assert generations["chair"].glb is None
    assert generations["chair"].png is None


@patch("submission_collector.download._fetch_glb", new_callable=AsyncMock, return_value=GLB_DATA)
@patch("submission_collector.download.render", new_callable=AsyncMock, return_value=None)
async def test_render_failure_saves_glb_without_png(
    mock_render: AsyncMock, mock_fetch: AsyncMock, git: MockGitHubClient, settings: Settings
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair"])
    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)

    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # Only GLB uploaded (no PNG)
    assert len(r2.uploads) == 1
    assert r2.uploads[0]["content_type"] == "application/octet-stream"

    # Generation has GLB but no PNG
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    generations = GenerationsAdapter.validate_json(git_batcher_committed(git)[gen_path])
    expected_glb = f"{settings.cdn_url}/rounds/1/{HOTKEY}/submitted/chair.glb"
    assert generations["chair"].glb == expected_glb
    assert generations["chair"].png is None
    assert generations["chair"].size == len(GLB_DATA)


def git_batcher_committed(git: MockGitHubClient) -> dict[str, str]:
    """Get all files written via git_batcher (which uses commit_files under the hood)."""
    return git.committed
