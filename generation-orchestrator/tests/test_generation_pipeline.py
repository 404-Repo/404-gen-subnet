from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from subnet_common.competition.generations import GenerationResult
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient
from subnet_common.testing.mock_r2 import MockR2Client

from generation_orchestrator.generate import GenerationResponse
from generation_orchestrator.generation_pipeline import GenerationPipeline
from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.pod_tracker import PodTracker
from generation_orchestrator.prompt_coordinator import PromptCoordinator, PromptEntry
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.settings import Settings


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def make_pipeline(
    settings: Settings,
    mock_git: MockGitHubClient,
    coordinator: PromptCoordinator,
) -> GenerationPipeline:
    git_batcher = GitBatcher(git=mock_git, branch="main", base_sha="abc123")
    return GenerationPipeline(
        settings=settings,
        pod_endpoint="http://fake-pod:10006",
        generation_token=None,
        coordinator=coordinator,
        git_batcher=git_batcher,
        hotkey="5abc123def",
        current_round=1,
        seed=42,
        stop=GenerationStop(),
    )


def make_coordinator() -> PromptCoordinator:
    return PromptCoordinator(
        max_attempts=3,
        lock_timeout=600,
        acceptable_distance=0.1,
        median_limit=90,
        hard_limit=180,
        median_trim_count=6,
    )


def make_tracker() -> PodTracker:
    return PodTracker(
        min_samples=8,
        failure_threshold=8,
        warmup_half_window=4,
        rolling_median_limit=120,
        hard_limit=180,
        retry_delta_min_samples=3,
    )


async def test_success(settings: Settings) -> None:
    prompt = Prompt(stem="prompt1", url="https://cdn/prompt1.jpg", path=FIXTURES_DIR / "prompt1.jpg")
    # Overtime prior result triggers retry path and distance measurement
    prior_result = GenerationResult(glb="x", png="x", generation_time=100.0, attempts=1)

    coordinator = make_coordinator()
    coordinator.seed(
        [PromptEntry(prompt=prompt, submitted_png="https://cdn/submitted.png")],
        {"prompt1": prior_result},
    )

    mock_git = MockGitHubClient()
    pipeline = make_pipeline(settings, mock_git, coordinator)
    tracker = make_tracker()

    mock_r2 = MockR2Client()
    gen_response = GenerationResponse(success=True, content=b"glb-data", generation_time=5.0)

    with (
        patch("generation_orchestrator.generation_pipeline.R2Client", return_value=mock_r2),
        patch("generation_orchestrator.generation_pipeline.generate", AsyncMock(return_value=gen_response)),
        patch("generation_orchestrator.generation_pipeline.render", AsyncMock(return_value=b"png-data")),
        patch("generation_orchestrator.generation_pipeline.measure_distance", AsyncMock(return_value=0.05)),
    ):
        await pipeline.run(pod_id="miner-01-5abc123def-0", tracker=tracker)

    assert coordinator.all_done()
    gen = coordinator.generations["prompt1"]
    assert gen.glb == "https://subnet404.xyz/rounds/1/5abc123def/generated/prompt1.glb"
    assert gen.png == "https://subnet404.xyz/rounds/1/5abc123def/generated/prompt1.png"
    assert gen.generation_time == 5.0
    assert gen.size == len(b"glb-data")
    assert gen.distance == 0.05
    assert gen.attempts == 2
    assert len(mock_r2.uploads) == 2

    assert tracker.retry_delta_count == 1
    assert tracker.retry_cumulative_delta == 5.0 - 100.0

    builds_path = "rounds/1/5abc123def/generated.json"
    assert builds_path in mock_git.committed
    saved = json.loads(mock_git.committed[builds_path])
    assert "prompt1" in saved


async def test_bad_pod_skips_generation(settings: Settings) -> None:
    prompt = Prompt(stem="prompt1", url="https://cdn/prompt1.jpg", path=FIXTURES_DIR / "prompt1.jpg")

    coordinator = make_coordinator()
    coordinator.seed([PromptEntry(prompt=prompt)], {})

    mock_git = MockGitHubClient()
    pipeline = make_pipeline(settings, mock_git, coordinator)
    tracker = make_tracker()
    tracker.mark_pod_bad("test", "test bad pod")

    mock_generate = AsyncMock()

    with (
        patch("generation_orchestrator.generation_pipeline.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.generation_pipeline.generate", mock_generate),
        patch("generation_orchestrator.generation_pipeline.render", AsyncMock()),
    ):
        await pipeline.run(pod_id="miner-01-5abc123def-0", tracker=tracker)

    mock_generate.assert_not_called()
    assert not coordinator.all_done()


async def test_mismatch_limit_skips_generation(settings: Settings) -> None:
    """When too many prompts have high distance, the loop exits without generating more."""
    prompt_path = FIXTURES_DIR / "prompt1.jpg"
    mismatched_result = GenerationResult(glb="x", png="x", generation_time=10.0, distance=0.5)

    # 2 * max_mismatched_prompts (6) = 12 mismatched prompts needed to trip the limit
    entries = []
    prior: dict[str, GenerationResult] = {}
    for i in range(12):
        stem = f"mismatched{i}"
        entries.append(PromptEntry(prompt=Prompt(stem=stem, url=f"https://cdn/{stem}.jpg", path=prompt_path)))
        prior[stem] = mismatched_result

    # One extra prompt that should NOT be generated
    entries.append(PromptEntry(prompt=Prompt(stem="extra", url="https://cdn/extra.jpg", path=prompt_path)))

    coordinator = make_coordinator()
    coordinator.seed(entries, prior)

    mock_git = MockGitHubClient()
    pipeline = make_pipeline(settings, mock_git, coordinator)
    tracker = make_tracker()

    mock_generate = AsyncMock()

    with (
        patch("generation_orchestrator.generation_pipeline.R2Client", return_value=MockR2Client()),
        patch("generation_orchestrator.generation_pipeline.generate", mock_generate),
        patch("generation_orchestrator.generation_pipeline.render", AsyncMock()),
    ):
        await pipeline.run(pod_id="miner-01-5abc123def-0", tracker=tracker)

    mock_generate.assert_not_called()
    assert tracker.termination_reason == "mismatch limit"
