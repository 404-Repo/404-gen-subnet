import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import bittensor as bt
import httpx
from loguru import logger
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.schedule import RoundSchedule, require_schedule
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.r2_client import R2Client

from submission_collector.download import DownloadPipeline
from submission_collector.prompts import select_prompts
from submission_collector.settings import Settings
from submission_collector.submission import Submission, parse_commitment


DownloadFn = Callable[[GitBatcher, CompetitionState, str, Settings], Awaitable[None]]
"""Signature: (git_batcher, state, ref, settings) -> None.
Creates R2/HTTP clients and runs the download pipeline.
"""


SECONDS_PER_BLOCK = 12
"""Average block time on Bittensor network."""


async def run_collection_iteration(settings: Settings) -> datetime | None:
    """Entry point: creates I/O dependencies and delegates to collection_iteration."""
    async with bt.async_subtensor(network=settings.network) as subtensor:

        async def get_block() -> int:
            block: int = await subtensor.get_current_block()
            return block

        async def get_commitments() -> dict:
            commitments: dict = await subtensor.get_all_revealed_commitments(netuid=settings.netuid)
            return commitments

        async def download_fn(git_batcher: GitBatcher, state: CompetitionState, ref: str, settings: Settings) -> None:
            async with (
                R2Client(
                    access_key_id=settings.r2_access_key_id.get_secret_value(),
                    secret_access_key=settings.r2_secret_access_key.get_secret_value(),
                    r2_endpoint=settings.r2_endpoint.get_secret_value(),
                ) as r2,
                httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as http_client,
            ):
                pipeline = DownloadPipeline(git_batcher=git_batcher, r2=r2, http_client=http_client, settings=settings)
                await pipeline.run(state=state, ref=ref)

        async with GitHubClient(
            repo=settings.github_repo,
            token=settings.github_token.get_secret_value(),
        ) as git:
            return await collection_iteration(
                git=git,
                get_block=get_block,
                get_commitments=get_commitments,
                download_fn=download_fn,
                settings=settings,
            )


async def collection_iteration(
    git: GitHubClient,
    get_block: Callable[[], Awaitable[int]],
    get_commitments: Callable[[], Awaitable[dict]],
    download_fn: DownloadFn,
    settings: Settings,
) -> datetime | None:
    """Run one submission-collector iteration.

    Handles three stages of the round lifecycle:
    - OPEN: Waits for latest_reveal_block, collects submissions from chain, saves to git
    - MINER_GENERATION: Waits for generation_deadline_block, then transitions
    - DOWNLOADING: Downloads 3D from miner CDNs, generates previews, saves to git

    Transitions to DUELS stage when downloading completes.

    Returns the estimated time for the next iteration, or None if the stage
    is not managed by this service.
    """
    ref = await git.get_ref_sha(ref=settings.github_branch)
    state = await require_state(git=git, ref=ref)
    logger.info(f"Commit: {ref[:10]}, state: {state}")

    if state.stage == RoundStage.OPEN:
        return await _collect_submissions(
            git=git, state=state, ref=ref, get_block=get_block, get_commitments=get_commitments, settings=settings
        )

    if state.stage == RoundStage.MINER_GENERATION:
        if eta := await _generation_deadline_eta(git=git, state=state, ref=ref, get_block=get_block):
            return eta
        ref = await _transition_stage(git=git, state=state, ref=ref, settings=settings, stage=RoundStage.DOWNLOADING)
        # Fall through to DOWNLOADING

    if state.stage == RoundStage.DOWNLOADING:
        await _handle_downloading(git=git, state=state, ref=ref, download_fn=download_fn, settings=settings)
        return None

    return state.next_stage_eta


async def _transition_stage(
    git: GitHubClient,
    state: CompetitionState,
    ref: str,
    settings: Settings,
    stage: RoundStage,
    next_stage_eta: datetime | None = None,
) -> str:
    """Update the competition state to a new stage and commit to git.
    Mutates state in place and persists the change.
    Returns the new commit SHA.
    """
    state.stage = stage
    state.next_stage_eta = next_stage_eta
    return await git.commit_files(
        files={"state.json": state.model_dump_json(indent=2)},
        message=f"Round {state.current_round}: {stage.value}",
        base_sha=ref,
        branch=settings.github_branch,
    )


async def _collect_submissions(
    git: GitHubClient,
    state: CompetitionState,
    ref: str,
    get_block: Callable[[], Awaitable[int]],
    get_commitments: Callable[[], Awaitable[dict]],
    settings: Settings,
) -> datetime | None:
    """Collect miner submissions from a chain after a reveal window closes.

    Waits until the latest_reveal_block is reached, then reads all valid submissions.
    If submissions exist, generates seed and prompts, saves everything to git,
    and transitions to MINER_GENERATION stage.
    If no submissions, transitions directly to FINALIZING.

    Returns ETA for next check if still waiting, or next stage ETA after transition.
    """
    schedule = await require_schedule(git=git, round_num=state.current_round, ref=ref)
    config = await require_competition_config(git=git, ref=ref)

    block = await get_block()
    if block < schedule.latest_reveal_block:
        return _get_block_eta(current_block=block, target_block=schedule.latest_reveal_block)

    submissions = await _read_submissions_from_chain(get_commitments=get_commitments, schedule=schedule)
    block = await get_block()

    logger.info(f"Collected {len(submissions)} submissions")

    if not submissions:
        await _transition_stage(git=git, state=state, ref=ref, settings=settings, stage=RoundStage.FINALIZING)
        return None

    seed = secrets.randbits(32)
    prompts = await select_prompts(git=git, round_num=state.current_round, config=config, seed=seed, ref=ref)

    state.stage = RoundStage.MINER_GENERATION
    state.next_stage_eta = _get_block_eta(current_block=block, target_block=schedule.generation_deadline_block)

    round_dir = f"rounds/{state.current_round}"
    await git.commit_files(
        files={
            "state.json": state.model_dump_json(indent=2),
            f"{round_dir}/seed.json": json.dumps({"seed": seed}),
            f"{round_dir}/prompts.txt": "\n".join(prompts),
            f"{round_dir}/submissions.json": _serialize_submissions(
                submissions=submissions, config=config, round_num=state.current_round
            ),
        },
        message=f"Seed, prompts and submissions for round {state.current_round}",
        base_sha=ref,
        branch=settings.github_branch,
    )

    return state.next_stage_eta


async def _generation_deadline_eta(
    git: GitHubClient,
    state: CompetitionState,
    ref: str,
    get_block: Callable[[], Awaitable[int]],
) -> datetime | None:
    """Check if the generation deadline has been reached.
    Returns ETA if still waiting for miners to generate, None if deadline passed,
    and we should proceed to downloading.
    """
    schedule = await require_schedule(git=git, round_num=state.current_round, ref=ref)

    block = await get_block()

    if block < schedule.generation_deadline_block:
        eta = _get_block_eta(current_block=block, target_block=schedule.generation_deadline_block)
        logger.info(f"Waiting for generation deadline: {eta}")
        return eta

    return None


async def _handle_downloading(
    git: GitHubClient,
    state: CompetitionState,
    ref: str,
    download_fn: DownloadFn,
    settings: Settings,
) -> None:
    """Run the download pipeline and transition to DUELS."""
    git_batcher = GitBatcher(git=git, base_sha=ref, branch=settings.github_branch)
    await download_fn(git_batcher, state, ref, settings)
    state.stage = RoundStage.DUELS
    state.next_stage_eta = None
    await git_batcher.write(
        path="state.json",
        content=state.model_dump_json(indent=2),
        message=f"Round {state.current_round}: {RoundStage.DUELS.value}",
    )
    await git_batcher.flush()


async def _read_submissions_from_chain(
    get_commitments: Callable[[], Awaitable[dict]],
    schedule: RoundSchedule,
) -> list[Submission]:
    """Fetch and parse revealed commitments from a chain within the reveal window.
    Returns submissions sorted by reveal block (earliest first).
    """
    commitments = await get_commitments()
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


def _serialize_submissions(submissions: list[Submission], config: CompetitionConfig, round_num: int) -> str:
    """Convert submissions to JSON for storage in git."""
    data = {
        s.hotkey: {
            "repo": s.repo,
            "commit": s.commit,
            "cdn_url": str(s.cdn_url),
            "revealed_at_block": s.reveal_block,
            "round": f"{config.name}-{round_num}",
        }
        for s in submissions
    }
    return json.dumps(data, indent=2)


def _get_block_eta(current_block: int, target_block: int) -> datetime:
    """Estimate UTC time when the target block will be reached."""
    blocks_remaining = target_block - current_block
    seconds_remaining = blocks_remaining * SECONDS_PER_BLOCK
    return datetime.now(UTC) + timedelta(seconds=seconds_remaining)
