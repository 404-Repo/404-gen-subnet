from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta

import bittensor as bt
from loguru import logger
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter, LeaderState, require_leader_state
from subnet_common.competition.round_result import RoundResult, require_round_result
from subnet_common.competition.schedule import RoundSchedule, get_schedule
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.github import GitHubClient

from round_manager.settings import Settings


BLOCK_TIME_SECONDS = 12


async def run_finalize_round(settings: Settings) -> None:
    async def get_block() -> int:
        async with bt.async_subtensor(network=settings.network) as subtensor:
            block: int = await subtensor.get_current_block()
        return block

    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        await finalize_round(git=git, get_block=get_block, settings=settings)


async def finalize_round(
    git: GitHubClient,
    get_block: Callable[[], Awaitable[int]],
    settings: Settings,
) -> None:
    ref = await git.get_ref_sha(ref=settings.github_branch)
    state: CompetitionState = await require_state(git=git, ref=ref)
    logger.info(f"Commit: {ref[:10]}, state: {state}")

    if state.stage != RoundStage.FINALIZING:
        return

    config = await require_competition_config(git, ref=ref)
    previous_schedule = await get_schedule(git, state.current_round, ref=ref)

    current_block = await get_block()
    current_time = datetime.now(UTC)

    previous_round_start = (
        _estimate_round_start(
            reveal_block=previous_schedule.latest_reveal_block,
            current_block=current_block,
            current_time=current_time,
            round_start_time=config.round_start_time,
        )
        if previous_schedule
        else None
    )

    next_round_start, next_round_start_block = _compute_next_round_start(
        current_time=current_time,
        current_block=current_block,
        config=config,
        previous_round_start=previous_round_start,
    )

    next_round_schedule = _build_next_schedule(
        next_round_start=next_round_start,
        next_round_start_block=next_round_start_block,
        config=config,
        previous_latest_reveal_block=previous_schedule.latest_reveal_block if previous_schedule else None,
    )

    leader_state = await require_leader_state(git, ref=ref)
    round_result = await require_round_result(git, round_num=state.current_round, ref=ref)

    current_leader = leader_state.get_latest()
    if current_leader is None:
        raise RuntimeError("Leader state is unexpectedly empty - competition must have a leader")

    leader_transition = _compute_leader_transition(
        round_result=round_result,
        current_leader=current_leader,
        config=config,
        effective_block=next_round_start_block,
    )

    if leader_transition:
        leader_state.transitions.append(leader_transition)

    next_stage = _determine_next_stage(next_round_schedule, settings.pause_on_stage_end)
    next_round = state.current_round if next_round_schedule is None else state.current_round + 1

    await _commit_round_updates(
        git=git,
        next_round=next_round,
        next_stage=next_stage,
        leader_state=leader_state,
        leader_changed=leader_transition is not None,
        next_round_schedule=next_round_schedule,
        latest_commit_sha=ref,
        next_stage_eta=next_round_start if next_round_schedule else None,
        branch=settings.github_branch,
    )


def _estimate_round_start(
    reveal_block: int,
    current_block: int,
    current_time: datetime,
    round_start_time: time,
) -> datetime:
    """
    Estimate round start datetime from a block number.

    Since rounds start at a configured time, we calculate the approximate date
    from block difference and combine it with the known start time.
    """
    blocks_ago = current_block - reveal_block
    approximate_time = current_time - timedelta(seconds=blocks_ago * BLOCK_TIME_SECONDS)
    return datetime.combine(approximate_time.date(), round_start_time, tzinfo=UTC)


def _compute_next_round_start(
    current_time: datetime,
    current_block: int,
    config: CompetitionConfig,
    previous_round_start: datetime | None,
) -> tuple[datetime, int]:
    """Calculate the next round start datetime and block."""
    earliest_date = (
        (previous_round_start + timedelta(days=config.round_duration_days)).date()
        if previous_round_start
        else current_time.date()
    )

    candidate = datetime.combine(earliest_date, config.round_start_time, tzinfo=UTC)
    logger.info(f"Initial candidate: {candidate}")

    candidate = _ensure_finalization_buffer(candidate, current_time, config.finalization_buffer_hours)
    candidate = _clamp_to_competition_start(candidate, config)

    seconds_until = (candidate - current_time).total_seconds()
    blocks_until = int(seconds_until / BLOCK_TIME_SECONDS)

    return candidate, current_block + blocks_until


def _ensure_finalization_buffer(candidate: datetime, current_time: datetime, buffer_hours: float) -> datetime:
    """Advance candidate date until there's sufficient finalization buffer."""
    min_buffer = timedelta(hours=buffer_hours)
    while (candidate - current_time) < min_buffer:
        candidate += timedelta(days=1)
    return candidate


def _clamp_to_competition_start(candidate: datetime, config: CompetitionConfig) -> datetime:
    """Ensure a candidate is not before the first evaluation date."""
    first_round = datetime.combine(config.first_evaluation_date, config.round_start_time, tzinfo=UTC)
    return max(candidate, first_round)


def _build_next_schedule(
    next_round_start: datetime,
    next_round_start_block: int,
    config: CompetitionConfig,
    previous_latest_reveal_block: int | None,
) -> RoundSchedule | None:
    """Build the schedule for the next round, or None if competition has ended."""
    if next_round_start.date() > config.last_competition_date:
        logger.info("Competition has ended. No more rounds to schedule.")
        return None

    logger.info(f"Next round start: ~{next_round_start}, block {next_round_start_block}")

    generation_blocks = int(config.generation_stage_minutes * 60 / BLOCK_TIME_SECONDS)
    generation_deadline_block = next_round_start_block + generation_blocks

    return RoundSchedule(
        earliest_reveal_block=previous_latest_reveal_block + 1 if previous_latest_reveal_block is not None else 0,
        latest_reveal_block=next_round_start_block,
        generation_deadline_block=generation_deadline_block,
    )


def _determine_next_stage(next_round_schedule: RoundSchedule | None, pause_on_stage_end: bool) -> RoundStage:
    """Determine what stage the competition should transition to."""
    if next_round_schedule is None:
        return RoundStage.FINISHED
    return RoundStage.PAUSED if pause_on_stage_end else RoundStage.OPEN


def _compute_leader_transition(
    round_result: RoundResult,
    current_leader: LeaderEntry,
    config: CompetitionConfig,
    effective_block: int,
) -> LeaderEntry | None:
    """
    Compute the leader transition entry for this round.

    Returns a new LeaderEntry to append to transitions, or None if no transition is needed.
    """
    new_leader = _find_new_leader(round_result, current_leader, effective_block)
    if new_leader:
        logger.info(f"New leader: {new_leader.hotkey} ({new_leader.repo}@{new_leader.commit[:8]})")
        return new_leader

    if current_leader.weight <= config.weight_floor:
        logger.info("Leader defended. Weight already at floor.")
        return None

    decayed_weight = max(config.weight_floor, current_leader.weight - config.weight_decay)
    logger.info(f"Leader defended. Weight decayed to {decayed_weight}")

    return current_leader.model_copy(
        update={
            "weight": decayed_weight,
            "effective_block": effective_block,
        }
    )


def _find_new_leader(
    round_result: RoundResult,
    current_leader: LeaderEntry,
    effective_block: int,
) -> LeaderEntry | None:
    """
    Determine if there's a new leader.

    Returns new LeaderEntry or None if the current leader defended.
    """
    if round_result.winner_hotkey == "leader":
        return None

    if round_result.repo == current_leader.repo and round_result.commit == current_leader.commit:
        return None

    return LeaderEntry(
        hotkey=round_result.winner_hotkey,
        repo=round_result.repo,
        commit=round_result.commit,
        docker=round_result.docker_image,
        weight=1.0,
        effective_block=effective_block,
    )


async def _commit_round_updates(
    git: GitHubClient,
    next_round: int,
    next_stage: RoundStage,
    leader_state: LeaderState,
    leader_changed: bool,
    next_round_schedule: RoundSchedule | None,
    latest_commit_sha: str,
    next_stage_eta: datetime | None,
    branch: str,
) -> None:
    """Commit all round updates atomically."""
    state = CompetitionState(stage=next_stage, current_round=next_round, next_stage_eta=next_stage_eta)

    files = {"state.json": state.model_dump_json(indent=2)}
    parts = [f"state ({next_stage.value})"]

    if next_round_schedule:
        files[f"rounds/{next_round}/schedule.json"] = next_round_schedule.model_dump_json(indent=2)
        parts.append("schedule")

    if leader_changed:
        files["leader.json"] = LeaderListAdapter.dump_json(leader_state.transitions, indent=2).decode()
        parts.append("leader")

    message = f"Update {', '.join(parts)} for round {next_round}"

    await git.commit_files(
        files=files,
        message=message,
        base_sha=latest_commit_sha,
        branch=branch,
    )
