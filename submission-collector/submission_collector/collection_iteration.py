import json

import bittensor as bt
from loguru import logger
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.schedule import RoundSchedule, require_schedule
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.github import GitHubClient
from subnet_common.utils import format_duration

from submission_collector.settings import settings
from submission_collector.submission import Submission, parse_commitment


SECONDS_PER_BLOCK = 12
"""Average block time on Bittensor network."""


async def run_collection_iteration() -> int | None:
    """Run one submission collection iteration.

    Waits for the reveal window to close, collects valid submissions from a chain,
    and commits results to Git.

    Returns seconds to wait before the next iteration, or None for a default interval.
    """
    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        latest_commit_sha = await git.get_ref_sha(ref=settings.github_branch)
        logger.info(f"Latest commit SHA: {latest_commit_sha}")

        state = await require_state(git, ref=latest_commit_sha)
        logger.info(f"Current state: {state}")

        if state.stage != RoundStage.COLLECTING:
            return None

        config = await require_competition_config(
            git=git,
            ref=latest_commit_sha,
        )
        logger.debug(f"Current competition config: {config}")

        schedule = await require_schedule(
            git=git,
            round_num=state.current_round,
            ref=latest_commit_sha,
        )
        logger.debug(f"Current schedule: {schedule}")

        async with bt.async_subtensor(network=settings.network) as subtensor:
            wait_seconds = await _get_wait_seconds(subtensor, schedule)
            if wait_seconds is not None:
                return wait_seconds

            submissions = await _collect_submissions(subtensor, schedule)

        logger.info(f"Collected submissions: {submissions}")

        next_stage = RoundStage.GENERATING if submissions else RoundStage.FINALIZING
        state.stage = RoundStage.PAUSED if settings.pause_on_stage_end else next_stage

        await _commit_state_and_submissions(
            git=git,
            base_sha=latest_commit_sha,
            state=state,
            submissions=submissions,
            config=config,
        )

        return None  # Wait default interval after a collection

async def _todo():
    latest_commit_sha = await _ensure_seed_and_prompts(
        git=git,
        state_round=state.current_round,
        config=competition_config,
        ref=latest_commit_sha,
    )
    git_batcher.base_sha = latest_commit_sha

async def _ensure_seed_and_prompts(
    git: GitHubClient,
    state_round: int,
    config: CompetitionConfig,
    ref: str,
) -> str:
    """Ensure seed and prompts exist for the round, creating them if needed.

    Returns the (possibly updated) commit ref.
    """
    seed = await get_seed_from_git(git, state_round, ref=ref)
    if seed is not None:
        return ref

    seed = secrets.randbits(32)
    logger.info(f"Generating seed: {seed}")

    prompts = await _select_round_prompts(git, state_round, config, seed, ref)

    new_sha: str = await git.commit_files(
        files={
            f"rounds/{state_round}/prompts.txt": "\n".join(prompts),
            f"rounds/{state_round}/seed.json": json.dumps({"seed": seed}),
        },
        message=f"Generate seed and pick prompts for round {state_round}",
        branch=settings.github_branch,
    )
    return new_sha


async def _select_round_prompts(
    git: GitHubClient,
    state_round: int,
    config: CompetitionConfig,
    seed: int,
    ref: str,
) -> list[str]:
    """Select prompts for the round: carryover from previous plus new from pool."""
    all_prompts = await require_prompts(git, round_num=None, ref=ref)
    previous_prompts = [] if state_round == 0 else await require_prompts(git, state_round - 1, ref=ref)

    rng = random.Random(seed)  # nosec B311 # noqa: S311 - deterministic selection, seed is secure

    carryover = _sample_up_to(rng, previous_prompts, config.carryover_prompts)
    new_count = config.prompts_per_round - len(carryover)
    new = _sample_up_to(rng, all_prompts, new_count)

    logger.info(f"Selected {len(carryover)} carryover + {len(new)} new prompts")
    return carryover + new


def _sample_up_to(rng: random.Random, items: list[str], k: int) -> list[str]:
    """Sample up to k items, returning all if fewer available."""
    if len(items) <= k:
        return items
    return rng.sample(items, k=k)


async def _get_wait_seconds(subtensor: bt.async_subtensor, schedule: RoundSchedule) -> int | None:
    """Return seconds to wait until a reveal window closes, or None if ready to collect."""
    current_block = await subtensor.get_current_block()
    target_block = schedule.latest_reveal_block + settings.submission_delay_blocks

    if current_block >= target_block:
        return None

    blocks_left = target_block - current_block
    seconds_left: int = blocks_left * SECONDS_PER_BLOCK

    logger.info(
        f"Block {current_block}/{target_block}, " f"{blocks_left} blocks left (~{format_duration(seconds_left)})"
    )

    return seconds_left


async def _collect_submissions(subtensor: bt.async_subtensor, schedule: RoundSchedule) -> list[Submission]:
    """Fetch and parse valid revealed commitments from a chain."""
    commitments = await subtensor.get_all_revealed_commitments(netuid=settings.netuid)
    logger.debug(f"Revealed commitments: {commitments}")

    submissions = [
        submission
        for hotkey, commitment in commitments.items()
        if (
            submission := parse_commitment(
                hotkey=hotkey,
                commitment=commitment,
                earliest_block=schedule.earliest_reveal_block,
                latest_block=schedule.latest_reveal_block,
            )
        )
    ]

    submissions.sort(key=lambda s: s.reveal_block)
    logger.success(f"Found {len(submissions)} valid submissions")

    return submissions


async def _commit_state_and_submissions(
    git: GitHubClient,
    base_sha: str,
    state: CompetitionState,
    submissions: list[Submission],
    config: CompetitionConfig,
) -> None:
    """Commit state and submissions to Git."""
    submissions_data = {
        submission.hotkey: {
            "repo": submission.repo,
            "commit": submission.commit,
            "round": f"{config.name}-{state.current_round}",
            "revealed_at_block": submission.reveal_block,
        }
        for submission in submissions
    }

    await git.commit_files(
        files={
            "state.json": state.model_dump_json(indent=2),
            f"rounds/{state.current_round}/submissions.json": json.dumps(submissions_data, indent=2),
        },
        message=f"Update state ({state.stage.value}) and submissions for round {state.current_round}",
        base_sha=base_sha,
        branch=settings.github_branch,
    )
