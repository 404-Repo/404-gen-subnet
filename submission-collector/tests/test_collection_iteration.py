import json

import pytest
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.testing import MockGitHubClient

from submission_collector.collection_iteration import collection_iteration
from submission_collector.settings import Settings
from tests.conftest import (
    SpyDownloadFn,
    add_config,
    add_prompts,
    add_schedule,
    add_state,
    make_commitment,
    make_get_block,
    make_get_commitments,
)


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.mark.parametrize(
    "stage",
    [RoundStage.DUELS, RoundStage.FINALIZING, RoundStage.FINISHED, RoundStage.PAUSED],
)
async def test_unhandled_stage_returns_next_stage_eta_and_does_nothing(
    git: MockGitHubClient, settings: Settings, stage: RoundStage
) -> None:
    add_state(git, stage=stage)
    download = SpyDownloadFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(),
        get_commitments=make_get_commitments(),
        download_fn=download,
        settings=settings,
    )

    assert result is None
    assert git.committed == {}
    assert not download.called


async def test_open_before_reveal_window_returns_eta(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=1)
    add_schedule(git, round_num=1, latest_reveal_block=200)
    add_config(git)
    download = SpyDownloadFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(100),
        get_commitments=make_get_commitments(),
        download_fn=download,
        settings=settings,
    )

    assert result is not None
    assert git.committed == {}
    assert not download.called


async def test_open_no_submissions_transitions_to_finalizing(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=1)
    add_schedule(git, round_num=1, latest_reveal_block=200)
    add_config(git)
    download = SpyDownloadFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments({}),
        download_fn=download,
        settings=settings,
    )

    assert result is None
    assert not download.called

    new_state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert new_state.stage == RoundStage.FINALIZING


async def test_miner_generation_before_deadline_returns_eta(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.MINER_GENERATION, round_num=1)
    add_schedule(git, round_num=1, generation_deadline_block=500)
    download = SpyDownloadFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(300),
        get_commitments=make_get_commitments(),
        download_fn=download,
        settings=settings,
    )

    assert result is not None
    assert git.committed == {}
    assert not download.called


async def test_miner_generation_past_deadline_transitions_to_downloading_and_calls_download(
    git: MockGitHubClient, settings: Settings
) -> None:
    add_state(git, stage=RoundStage.MINER_GENERATION, round_num=1)
    add_schedule(git, round_num=1, generation_deadline_block=500)
    download = SpyDownloadFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(500),
        get_commitments=make_get_commitments(),
        download_fn=download,
        settings=settings,
    )

    assert result is None
    assert download.called

    # First commit: MINER_GENERATION -> DOWNLOADING transition
    first_commit = git._commits[0]
    downloading_state = CompetitionState.model_validate_json(first_commit["files"]["state.json"])
    assert downloading_state.stage == RoundStage.DOWNLOADING

    # Last commit: DOWNLOADING -> DUELS transition
    last_commit = git._commits[-1]
    duels_state = CompetitionState.model_validate_json(last_commit["files"]["state.json"])
    assert duels_state.stage == RoundStage.DUELS


async def test_open_collects_submissions_and_transitions_to_miner_generation(
    git: MockGitHubClient, settings: Settings
) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=500)
    add_config(git, prompts_per_round=3, carryover_prompts=0)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c", "prompt_d", "prompt_e"])

    commitments = {HOTKEY: make_commitment(block=150)}
    download = SpyDownloadFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments(commitments),
        download_fn=download,
        settings=settings,
    )

    # Should return an ETA for the generation deadline
    assert result is not None
    assert not download.called

    # Should commit state, seed, prompts, and submissions
    committed = git.committed
    assert "state.json" in committed
    assert "rounds/0/seed.json" in committed
    assert "rounds/0/prompts.txt" in committed
    assert "rounds/0/submissions.json" in committed

    # State should transition to MINER_GENERATION
    new_state = CompetitionState.model_validate_json(committed["state.json"])
    assert new_state.stage == RoundStage.MINER_GENERATION
    assert new_state.next_stage_eta is not None

    # Seed should be present
    seed_data = json.loads(committed["rounds/0/seed.json"])
    assert "seed" in seed_data
    assert isinstance(seed_data["seed"], int)

    # Prompts should be selected (3 as configured)
    prompts = committed["rounds/0/prompts.txt"].strip().splitlines()
    assert len(prompts) == 3

    # Submissions should contain the hotkey
    submissions = json.loads(committed["rounds/0/submissions.json"])
    assert HOTKEY in submissions
    assert submissions[HOTKEY]["repo"] == "test/model"
    assert submissions[HOTKEY]["commit"] == "a" * 40
