from datetime import UTC, date, datetime, time

import pytest
import time_machine
from subnet_common.competition.config import CompetitionConfig
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter
from subnet_common.competition.round_result import RoundResult
from subnet_common.competition.schedule import RoundSchedule
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.testing import MockGitHubClient

from round_manager.finalize_round import finalize_round
from round_manager.settings import Settings
from tests.conftest import make_get_block


def add_state(
    git: MockGitHubClient,
    stage: RoundStage = RoundStage.FINALIZING,
    current_round: int = 1,
    **kwargs,
) -> CompetitionState:
    state = CompetitionState(stage=stage, current_round=current_round, **kwargs)
    git.files["state.json"] = state.model_dump_json()
    return state


def add_config(git: MockGitHubClient, **overrides) -> CompetitionConfig:
    defaults = {
        "name": "Test Competition",
        "description": "Test description",
        "first_evaluation_date": date(2000, 1, 1),
        "last_competition_date": date(3000, 12, 31),
        "round_start_time": time(8, 0),
        "generation_stage_minutes": 120,
        "finalization_buffer_hours": 1.0,
        "win_margin": 0.05,
        "weight_decay": 0.2,
        "weight_floor": 0.5,
        "prompts_per_round": 10,
        "carryover_prompts": 5,
        "round_duration_days": 1,
    }
    config = CompetitionConfig(**{**defaults, **overrides})
    git.files["config.json"] = config.model_dump_json()
    return config


def add_schedule(git: MockGitHubClient, round_num: int = 0, **overrides) -> RoundSchedule:
    defaults = {
        "earliest_reveal_block": 0,
        "latest_reveal_block": hours_to_blocks(24),
        "generation_deadline_block": hours_to_blocks(26),
    }
    schedule = RoundSchedule(**{**defaults, **overrides})
    git.files[f"rounds/{round_num}/schedule.json"] = schedule.model_dump_json()
    return schedule


def add_leader(git: MockGitHubClient, **overrides) -> LeaderEntry:
    defaults = {
        "hotkey": "leader_hotkey",
        "repo": "leader/repo",
        "commit": "abc123",
        "docker": "leader:latest",
        "weight": 1.0,
        "effective_block": 0,
    }
    leader = LeaderEntry(**{**defaults, **overrides})
    git.files["leader.json"] = LeaderListAdapter.dump_json([leader]).decode()
    return leader


def add_round_result(git: MockGitHubClient, round_num: int = 0, **overrides) -> RoundResult:
    defaults = {
        "winner_hotkey": "leader_hotkey",
        "repo": "leader/repo",
        "commit": "abc123",
        "docker_image": "leader:latest",
    }
    result = RoundResult(**{**defaults, **overrides})
    git.files[f"rounds/{round_num}/winner.json"] = result.model_dump_json()
    return result


def hours_to_blocks(hours: int) -> int:
    return int(hours * 60 * 5)


@time_machine.travel(datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC), tick=False)
async def test_new_leader_takes_over(git: MockGitHubClient, settings: Settings) -> None:
    """Miner wins with different repo, becomes new leader with weight 1.0."""
    add_state(git, current_round=1)
    add_config(git)
    schedule = add_schedule(git, round_num=1)
    add_leader(git, weight=1.0)
    add_round_result(git, round_num=1, winner_hotkey="miner_hotkey", repo="miner/repo")

    await finalize_round(git=git, get_block=make_get_block(block=7200), settings=settings)

    committed = git.committed

    assert "state.json" in committed
    assert "rounds/2/schedule.json" in committed
    assert "leader.json" in committed

    state = CompetitionState.model_validate_json(committed["state.json"])
    assert state == CompetitionState(
        stage=RoundStage.OPEN,
        current_round=2,
        next_stage_eta=datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
    )

    next_schedule = RoundSchedule.model_validate_json(committed["rounds/2/schedule.json"])
    assert next_schedule == RoundSchedule(
        earliest_reveal_block=schedule.latest_reveal_block + 1,
        latest_reveal_block=schedule.latest_reveal_block + hours_to_blocks(24),
        generation_deadline_block=schedule.latest_reveal_block + hours_to_blocks(26),
    )

    leaders = LeaderListAdapter.validate_json(committed["leader.json"])
    assert leaders[-1].hotkey == "miner_hotkey"
    assert leaders[-1].weight == 1.0


async def test_early_exit_when_not_finalizing(git: MockGitHubClient, settings: Settings) -> None:
    """Should return early without committing if stage is not FINALIZING."""
    add_state(git, stage=RoundStage.OPEN)

    await finalize_round(git=git, get_block=make_get_block(), settings=settings)

    assert len(git.committed) == 0


@time_machine.travel(datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC), tick=False)
async def test_next_round_pushed_by_finalization_buffer(git: MockGitHubClient, settings: Settings) -> None:
    """Candidate Jan 2 is only 24h away, but buffer requires 25h → pushed to Jan 3."""
    add_state(git, current_round=1)
    add_config(git, finalization_buffer_hours=25)
    schedule = add_schedule(git, round_num=1)
    add_leader(git, weight=1.0)
    add_round_result(git, round_num=1, winner_hotkey="miner_hotkey", repo="miner/repo", commit="def456")

    await finalize_round(git=git, get_block=make_get_block(block=7200), settings=settings)

    committed = git.committed

    state = CompetitionState.model_validate_json(committed["state.json"])
    assert state == CompetitionState(
        stage=RoundStage.OPEN,
        current_round=2,
        next_stage_eta=datetime(2026, 1, 3, 8, 0, tzinfo=UTC),
    )

    # 48h away = 14400 blocks, next_start_block = 7200 + 14400 = 21600
    next_schedule = RoundSchedule.model_validate_json(committed["rounds/2/schedule.json"])
    assert next_schedule == RoundSchedule(
        earliest_reveal_block=schedule.latest_reveal_block + 1,
        latest_reveal_block=schedule.latest_reveal_block + hours_to_blocks(48),
        generation_deadline_block=schedule.latest_reveal_block + hours_to_blocks(50),
    )


@time_machine.travel(datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC), tick=False)
async def test_competition_ended(git: MockGitHubClient, settings: Settings) -> None:
    """Competition ends when the next round would start after the last_competition_date."""
    add_state(git, current_round=1)
    add_config(git, last_competition_date=date(2026, 1, 1))
    add_schedule(git, round_num=1)
    add_leader(git, weight=1.0)
    add_round_result(git, round_num=1, winner_hotkey="leader")

    await finalize_round(git=git, get_block=make_get_block(block=7200), settings=settings)

    committed = git.committed

    state = CompetitionState.model_validate_json(committed["state.json"])
    assert state == CompetitionState(
        stage=RoundStage.FINISHED,
        current_round=1,
        next_stage_eta=None,
    )
    assert "rounds/2/schedule.json" not in committed


@time_machine.travel(datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC), tick=False)
async def test_leader_defends_weight_decays(git: MockGitHubClient, settings: Settings) -> None:
    """Leader defends, weight above floor → weight decays."""
    add_state(git, current_round=1)
    add_config(git)
    add_schedule(git, round_num=1)
    add_leader(git, weight=1.0)
    add_round_result(git, round_num=1, winner_hotkey="leader")

    await finalize_round(git=git, get_block=make_get_block(block=7200), settings=settings)

    committed = git.committed
    assert "leader.json" in committed

    leaders = LeaderListAdapter.validate_json(committed["leader.json"])
    assert len(leaders) == 2  # original + decay entry
    assert leaders[-1].hotkey == "leader_hotkey"
    assert leaders[-1].weight == pytest.approx(0.8)


@time_machine.travel(datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC), tick=False)
async def test_leader_defends_weight_at_floor(git: MockGitHubClient, settings: Settings) -> None:
    """Leader defends, weight already at floor → no leader transition committed."""
    add_state(git, current_round=1)
    add_config(git)
    add_schedule(git, round_num=1)
    add_leader(git, weight=0.2)
    add_round_result(git, round_num=1, winner_hotkey="leader")

    await finalize_round(git=git, get_block=make_get_block(block=7200), settings=settings)

    committed = git.committed
    assert "leader.json" not in committed


@time_machine.travel(datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC), tick=False)
async def test_leader_remains_same_repo_and_commit(git: MockGitHubClient, settings: Settings) -> None:
    """Different hotkey wins but same repo+commit as leader → treated as defense, weight decays."""
    add_state(git, current_round=1)
    add_config(git)
    add_schedule(git, round_num=1)
    add_leader(git, weight=0.8, repo="shared/repo", commit="same_hash")
    add_round_result(
        git,
        round_num=1,
        winner_hotkey="different_miner",
        repo="shared/repo",
        commit="same_hash",
    )

    await finalize_round(git=git, get_block=make_get_block(block=7200), settings=settings)

    committed = git.committed
    assert "leader.json" in committed

    leaders = LeaderListAdapter.validate_json(committed["leader.json"])
    assert leaders[-1].hotkey == "leader_hotkey"  # original leader kept
    assert leaders[-1].weight == pytest.approx(0.6)
