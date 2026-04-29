import json
from unittest.mock import AsyncMock, patch

import httpx
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.render import GRAY_VIEWS, WHITE_VIEWS
from subnet_common.testing import MockGitHubClient, MockR2Client

from submission_collector.download import DownloadPipeline
from submission_collector.settings import Settings


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
REF = "abc123"
JS_DATA = b"fake-js-data"
PNG = b"fake-png-data"
GRID_PNG = b"fake-grid-data"
NPZ_BYTES = b"fake-npz-data"
PROMPT_IMG = b"fake-prompt-image"
# render_views mock return — covers both WHITE_VIEWS and GRAY_VIEWS lookups.
VIEW_PNGS = {v.name: PNG for v in (*WHITE_VIEWS, *GRAY_VIEWS)}
TOTAL_VIEW_UPLOADS = len(WHITE_VIEWS) + len(GRAY_VIEWS)  # 12
# 1 JS + 12 views + 1 grid + 1 embeddings.npz
TOTAL_BUNDLE_UPLOADS = 1 + TOTAL_VIEW_UPLOADS + 2


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


@patch(
    "submission_collector.download.DownloadPipeline._fetch_prompt_images",
    new_callable=AsyncMock,
    return_value={"chair": PROMPT_IMG, "table": PROMPT_IMG},
)
@patch("submission_collector.download._build_embeddings_npz", new_callable=AsyncMock, return_value=NPZ_BYTES)
@patch("submission_collector.download.render_grid", new_callable=AsyncMock, return_value=GRID_PNG)
@patch("submission_collector.download._fetch_js", new_callable=AsyncMock, return_value=JS_DATA)
@patch("submission_collector.download.render_views", new_callable=AsyncMock, return_value=VIEW_PNGS)
async def test_happy_path_fetches_renders_uploads(
    mock_render: AsyncMock,
    mock_fetch: AsyncMock,
    mock_grid: AsyncMock,
    mock_npz: AsyncMock,
    mock_prompts: AsyncMock,
    git: MockGitHubClient,
    settings: Settings,
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair"])
    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)

    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # JS fetched from miner CDN
    mock_fetch.assert_called_once()
    assert "chair" in str(mock_fetch.call_args)

    # render_views called twice (one per bg color) with the same JS
    assert mock_render.call_count == 2
    assert mock_render.call_args_list[0].kwargs["js_content"] == JS_DATA
    assert mock_render.call_args_list[1].kwargs["js_content"] == JS_DATA
    mock_grid.assert_called_once()
    mock_npz.assert_called_once()

    # 1 JS + 12 view PNGs + 1 grid + 1 embeddings.npz
    assert len(r2.uploads) == TOTAL_BUNDLE_UPLOADS
    assert r2.uploads[0]["content_type"] == "application/javascript"
    # The .npz upload uses application/octet-stream; everything else is image/png
    npz_uploads = [u for u in r2.uploads if u["content_type"] == "application/octet-stream"]
    png_uploads = [u for u in r2.uploads if u["content_type"] == "image/png"]
    assert len(npz_uploads) == 1
    assert len(png_uploads) == TOTAL_VIEW_UPLOADS + 1  # views + grid

    # Generation result saved to git
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    assert gen_path in git_batcher_committed(git)
    generations = GenerationsAdapter.validate_json(git_batcher_committed(git)[gen_path])
    assert "chair" in generations
    expected_prefix = f"{settings.cdn_url}/rounds/1/{HOTKEY}/submitted/chair"
    assert generations["chair"].js == f"{expected_prefix}.js"
    assert generations["chair"].views == expected_prefix
    assert generations["chair"].size == len(JS_DATA)


@patch(
    "submission_collector.download.DownloadPipeline._fetch_prompt_images",
    new_callable=AsyncMock,
    return_value={"chair": PROMPT_IMG, "table": PROMPT_IMG},
)
@patch("submission_collector.download._build_embeddings_npz", new_callable=AsyncMock, return_value=NPZ_BYTES)
@patch("submission_collector.download.render_grid", new_callable=AsyncMock, return_value=GRID_PNG)
@patch("submission_collector.download._fetch_js", new_callable=AsyncMock, return_value=JS_DATA)
@patch("submission_collector.download.render_views", new_callable=AsyncMock, return_value=VIEW_PNGS)
async def test_skips_already_completed_prompts(
    mock_render: AsyncMock,
    mock_fetch: AsyncMock,
    mock_grid: AsyncMock,
    mock_npz: AsyncMock,
    mock_prompts: AsyncMock,
    git: MockGitHubClient,
    settings: Settings,
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair", "table"])

    # "chair" already completed
    existing = {"chair": GenerationResult(js="https://cdn/chair.js", views="https://cdn/chair", size=100)}
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    git.files[gen_path] = GenerationsAdapter.dump_json(existing).decode()

    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)
    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # Only "table" should be fetched
    mock_fetch.assert_called_once()
    assert "table" in str(mock_fetch.call_args)


@patch(
    "submission_collector.download.DownloadPipeline._fetch_prompt_images",
    new_callable=AsyncMock,
    return_value={"chair": PROMPT_IMG},
)
@patch("submission_collector.download._build_embeddings_npz", new_callable=AsyncMock, return_value=NPZ_BYTES)
@patch("submission_collector.download.render_grid", new_callable=AsyncMock, return_value=GRID_PNG)
@patch("submission_collector.download._fetch_js", new_callable=AsyncMock, side_effect=Exception("CDN down"))
@patch("submission_collector.download.render_views", new_callable=AsyncMock, return_value=VIEW_PNGS)
async def test_fetch_failure_records_empty_generation(
    mock_render: AsyncMock,
    mock_fetch: AsyncMock,
    mock_grid: AsyncMock,
    mock_npz: AsyncMock,
    mock_prompts: AsyncMock,
    git: MockGitHubClient,
    settings: Settings,
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair"])
    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)

    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # No uploads to R2
    assert len(r2.uploads) == 0

    # render_views not called
    mock_render.assert_not_called()

    # Empty generation result saved
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    generations = GenerationsAdapter.validate_json(git_batcher_committed(git)[gen_path])
    assert "chair" in generations
    assert generations["chair"].js is None
    assert generations["chair"].views is None


@patch(
    "submission_collector.download.DownloadPipeline._fetch_prompt_images",
    new_callable=AsyncMock,
    return_value={"chair": PROMPT_IMG},
)
@patch("submission_collector.download._build_embeddings_npz", new_callable=AsyncMock, return_value=NPZ_BYTES)
@patch("submission_collector.download.render_grid", new_callable=AsyncMock, return_value=GRID_PNG)
@patch("submission_collector.download._fetch_js", new_callable=AsyncMock, return_value=JS_DATA)
@patch("submission_collector.download.render_views", new_callable=AsyncMock, return_value=None)
async def test_render_failure_saves_js_without_views(
    mock_render: AsyncMock,
    mock_fetch: AsyncMock,
    mock_grid: AsyncMock,
    mock_npz: AsyncMock,
    mock_prompts: AsyncMock,
    git: MockGitHubClient,
    settings: Settings,
) -> None:
    r2 = MockR2Client()
    _setup_round(git, round_num=1, hotkeys=[HOTKEY], prompts=["chair"])
    state = CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING)

    pipeline = _make_pipeline(git, r2, settings)
    await pipeline.run(state=state, ref=REF)

    # Only the JS got uploaded — render_views returned None so no view/grid/npz uploads.
    assert len(r2.uploads) == 1
    assert r2.uploads[0]["content_type"] == "application/javascript"

    # Generation has JS but no views
    gen_path = f"rounds/1/{HOTKEY}/submitted.json"
    generations = GenerationsAdapter.validate_json(git_batcher_committed(git)[gen_path])
    expected_js = f"{settings.cdn_url}/rounds/1/{HOTKEY}/submitted/chair.js"
    assert generations["chair"].js == expected_js
    assert generations["chair"].views is None
    assert generations["chair"].size == len(JS_DATA)


def git_batcher_committed(git: MockGitHubClient) -> dict[str, str]:
    """Get all files written via git_batcher (which uses commit_files under the hood)."""
    return git.committed
