from datetime import UTC, datetime, timedelta

import bittensor as bt
from loguru import logger
from subnet_common.competition.build_info import BuildInfo, get_builds
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.judge_progress import JudgeProgress, get_judge_progress
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter, LeaderState, require_leader_state
from subnet_common.competition.schedule import RoundSchedule, get_schedule
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.github import GitHubClient

from round_manager.settings import settings


BLOCK_TIME_SECONDS = 12


async def run_manage_round_iteration() -> None:
    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        latest_commit_sha = await git.get_ref_sha(ref=settings.github_branch)
        logger.info(f"Latest commit SHA: {latest_commit_sha}")

        state = await require_state(git, ref=latest_commit_sha)
        logger.info(f"Current state: {state}")

        if state.stage != RoundStage.FINALIZING:
            return

        config = await require_competition_config(git, ref=latest_commit_sha)
        logger.debug(f"Current competition config: {config}")

        schedule = await get_schedule(git, state.current_round, ref=latest_commit_sha)
        logger.debug(f"Previous round schedule: {schedule}")

        subtensor = await bt.get_async_subtensor(network=settings.network)
        current_block = await subtensor.get_current_block()

        previous_round_start: datetime | None = None

        if schedule:
            previous_round_start = await _get_previous_round_start(latest_reveal_block=schedule.latest_reveal_block, subtensor=subtensor)

        logger.debug(f"Previous round start: {previous_round_start}")

        next_round_start, next_round_start_block = _get_next_round_start(
            current_time=datetime.now(UTC),
            current_block=current_block,
            config=config,
            previous_round_start=previous_round_start,
        )

        # Check if the competition has ended
        if next_round_start.date() > config.last_competition_date:
            logger.info("Competition has ended. No more rounds to schedule.")
            next_round_schedule = None
        else:
            logger.info(f"Next round start: ~{next_round_start}, block {next_round_start_block}")
            next_round_schedule = RoundSchedule(
                earliest_reveal_block=schedule.latest_reveal_block + 1 if schedule else 0,
                latest_reveal_block=next_round_start_block,
            )

        leader_state = await require_leader_state(git, ref=latest_commit_sha)
        judge_progress = await get_judge_progress(git, state.current_round, ref=latest_commit_sha)
        logger.debug(f"Judge progress for previous round: {judge_progress}")

        builds = await get_builds(git, round_num=state.current_round, ref=latest_commit_sha)
        leader_state = update_leader_state(
            leader_state=leader_state,
            judge_progress=judge_progress,
            builds=builds,
            config=config,
            effective_block=next_round_start_block,
        )

        if next_round_schedule is None:
            state.stage = RoundStage.FINISHED
        else:
            state.stage = RoundStage.PAUSED if settings.pause_on_stage_end else RoundStage.COLLECTING
            state.current_round += 1

        await commit_round_updates(
            git=git,
            state=state,
            leader_state=leader_state,
            next_round_schedule=next_round_schedule,
            latest_commit_sha=latest_commit_sha,
        )


def _get_next_round_start(
    current_time: datetime,
    current_block: int,
    config: CompetitionConfig,
    previous_round_start: datetime | None,
) -> tuple[datetime, int]:
    """
    Calculate the next round start datetime and block.
    """

    # Find earliest eligible date based on previous round
    if previous_round_start is not None:
        earliest_date = (previous_round_start + timedelta(days=config.round_duration_days)).date()
    else:
        earliest_date = current_time.date()

    # Build candidate at the fixed round start time
    candidate = datetime.combine(earliest_date, config.round_start_time, tzinfo=UTC)

    logger.info(f"Candidate: {candidate}")

    # Skip days if insufficient finalization buffer
    time_remaining = candidate - current_time
    min_time_remaining = timedelta(hours=config.finalization_buffer_hours)
    while time_remaining < min_time_remaining:
        candidate += timedelta(days=1)
        time_remaining = candidate - current_time

    # Clamp to competition bounds
    first_round = datetime.combine(config.first_evaluation_date, config.round_start_time, tzinfo=UTC)
    if candidate < first_round:
        candidate = first_round

    # Calculate block
    seconds_until = (candidate - current_time).total_seconds()
    blocks_until = int(seconds_until / BLOCK_TIME_SECONDS)

    return candidate, current_block + blocks_until


async def _get_previous_round_start(latest_reveal_block: int, subtensor: bt.AsyncSubtensor) -> datetime:
    """
    Get the previous round start datetime.
    """
    try:
        block_timestamp = await subtensor.get_timestamp(block=latest_reveal_block)
        return block_timestamp
    except Exception as e:
        logger.warning(f"Error getting previous round start: {e}. Use archive.")
        archive_subtensor = await bt.get_async_subtensor(network="archive")
        timestamp = await archive_subtensor.get_timestamp(block=latest_reveal_block)
        return timestamp


def update_leader_state(
    leader_state: LeaderState,
    judge_progress: JudgeProgress,
    builds: dict[str, BuildInfo],
    config: CompetitionConfig,
    effective_block: int,
) -> LeaderState | None:
    """
    Update the leader state based on round results.

    Returns updated LeaderState or None if no changes are needed.
    """
    current_leader = leader_state.get_latest()
    new_leader = find_new_leader(judge_progress, builds, current_leader, effective_block)

    if new_leader:
        logger.info(f"New leader: {new_leader.hotkey} ({new_leader.repo}@{new_leader.commit[:8]})")
        leader_state.transitions.append(new_leader)
        return leader_state

    if current_leader.weight <= config.weight_floor:
        logger.info("Leader defended. Weight already at floor.")
        return None

    decayed_weight = max(config.weight_floor, current_leader.weight - config.weight_decay)
    decayed_leader = current_leader.model_copy(update={"weight": decayed_weight, "effective_block": effective_block})
    leader_state.transitions.append(decayed_leader)
    logger.info(f"Leader defended. Weight decayed to {decayed_weight}")
    return leader_state


def find_new_leader(
    judge_progress: JudgeProgress,
    builds: dict[str, BuildInfo],
    current_leader: LeaderEntry,
    effective_block: int,
) -> LeaderEntry | None:
    """
    Determine if there's a new leader.

    Returns new LeaderEntry or None if the current leader is defended.
    """
    winner = judge_progress.leader

    # "leader" means the current leader won
    if winner == "leader":
        return None

    # Winner not in builds - invalid
    if winner not in builds:
        logger.warning(f"Winner {winner} not found in builds")
        return None

    build = builds[winner]

    # Same repo and commit as current leader - no change
    if build.repo == current_leader.repo and build.commit == current_leader.commit:
        return None

    return LeaderEntry(
        hotkey=winner,
        repo=build.repo,
        commit=build.commit,
        docker=build.docker_image,
        weight=1.0,
        effective_block=effective_block,
    )


async def commit_round_updates(
    git: GitHubClient,
    state: CompetitionState,
    leader_state: LeaderState | None,
    next_round_schedule: RoundSchedule | None,
    latest_commit_sha: str,
) -> None:
    """Commit all round updates atomically."""
    files = {"state.json": state.model_dump_json(indent=2)}
    parts = [f"state ({state.stage.value})"]

    if next_round_schedule:
        files[f"rounds/{state.current_round}/schedule.json"] = next_round_schedule.model_dump_json(indent=2)
        parts.append("schedule")

    if leader_state:
        files["leader.json"] = LeaderListAdapter.dump_json(leader_state.transitions, indent=2).decode()
        parts.append("leader")

    message = f"Update {', '.join(parts)} for round {state.current_round}"

    await git.commit_files(
        files=files,
        message=message,
        base_sha=latest_commit_sha,
        branch=settings.github_branch,
    )
