import asyncio
import json
import random
import secrets

from loguru import logger
from subnet_common.competition.build_info import BuildInfo, BuildStatus, save_builds
from subnet_common.competition.config import CompetitionConfig, require_competition_config
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.seed import get_seed_from_git, require_seed_from_git
from subnet_common.competition.state import RoundStage, require_state, update_competition_state
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient, GitHubJob
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.miner_process import process_miner_with_retries
from generation_orchestrator.prompts import Prompt, ensure_prompts_cached_from_git
from generation_orchestrator.settings import settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore
from generation_orchestrator.targon_client import TargonClient


TERMINAL_BUILD_STATUSES = frozenset(
    {
        BuildStatus.SUCCESS,
        BuildStatus.FAILURE,
        BuildStatus.NOT_FOUND,
        BuildStatus.TIMED_OUT,
    }
)
"""Build statuses that indicate no further progress is expected."""


async def run_generation_iteration(shutdown: GracefulShutdown) -> None:
    """Run one generation iteration: build miner images and generate 3D models.

    Loads competition state from Git, initializes prompts/seed if needed,
    watches Docker builds, spawns generation tasks, and persists results.
    """

    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        latest_commit_sha = await git.get_ref_sha(ref=settings.github_branch)
        logger.info(f"Latest commit SHA: {latest_commit_sha}")

        state = await require_state(git, ref=latest_commit_sha)
        logger.info(f"Current state: {state}")

        if state.stage != RoundStage.GENERATING:
            return

        submissions = await require_submissions(git, state.current_round, ref=latest_commit_sha)
        logger.debug(f"Submissions: {submissions}")

        git_batcher = await GitBatcher.create(git=git, branch=settings.github_branch, base_sha=latest_commit_sha)

        if not submissions:
            logger.info("No submissions for the round")
            state.stage = RoundStage.PAUSED if settings.pause_on_stage_end else RoundStage.FINALIZING
            await update_competition_state(git_batcher, state)
            await git_batcher.flush()
            return

        competition_config = await require_competition_config(git, ref=latest_commit_sha)

        latest_commit_sha = await _ensure_seed_and_prompts(
            git=git,
            state_round=state.current_round,
            config=competition_config,
            ref=latest_commit_sha,
        )
        git_batcher.base_sha = latest_commit_sha

        seed = await require_seed_from_git(git, state.current_round, ref=latest_commit_sha)
        logger.info(f"Seed: {seed}")

        prompts = await ensure_prompts_cached_from_git(
            git=git,
            cache_dir=settings.cache_dir,
            round_num=state.current_round,
            ref=latest_commit_sha,
        )

        run_id = await git.find_run_by_commit_message(
            settings.build_commit_message.format(current_round=state.current_round)
        )
        if run_id is None:
            logger.warning("No git run found for the round, waiting...")
            return

        leader_docker_image = await _get_leader_docker_image(git, latest_commit_sha)

        await _watch_builds_and_generate(
            git_batcher=git_batcher,
            run_id=run_id,
            current_round=state.current_round,
            submissions=submissions,
            prompts=prompts,
            seed=seed,
            leader_docker_image=leader_docker_image,
            shutdown=shutdown,
        )

        if not shutdown.should_stop:
            state.stage = RoundStage.PAUSED if settings.pause_on_stage_end else RoundStage.DUELS
            await update_competition_state(git_batcher, state)

        await git_batcher.flush()


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


async def _get_leader_docker_image(git: GitHubClient, ref: str) -> str | None:
    """Get leader docker image"""
    leader_state = await require_leader_state(git, ref)
    leader = leader_state.get_latest()
    return leader.docker if leader else None


async def _watch_builds_and_generate(
    git_batcher: GitBatcher,
    run_id: int,
    current_round: int,
    submissions: dict[str, MinerSubmission],
    prompts: list[Prompt],
    seed: int,
    shutdown: GracefulShutdown,
    leader_docker_image: str | None = None,
) -> None:
    """Watch builds, spawn generations on success."""

    builds = {hotkey: BuildInfo.from_submission(submission) for hotkey, submission in submissions.items()}
    tasks: dict[str, asyncio.Task] = {}  # hotkey -> task
    semaphore = StaggeredSemaphore(settings.max_concurrent_miners)
    deadline = asyncio.get_running_loop().time() + settings.build_timeout_seconds

    if leader_docker_image:
        tasks["leader"] = asyncio.create_task(
            process_miner_with_retries(
                semaphore=semaphore,
                git_batcher=git_batcher,
                hotkey="leader",
                docker_image=leader_docker_image,
                current_round=current_round,
                prompts=prompts,
                seed=seed,
                shutdown=shutdown,
            ),
            name="leader",
        )

    while not shutdown.should_stop:
        jobs = await _get_miner_build_jobs(git_batcher.git, run_id)
        changes = _update_builds(builds, jobs)

        # Spawn generation for newly successful builds
        for hotkey in changes:
            build = builds[hotkey]
            if build.status == BuildStatus.SUCCESS and build.docker_image is not None:
                tasks[hotkey] = asyncio.create_task(
                    process_miner_with_retries(
                        semaphore=semaphore,
                        git_batcher=git_batcher,
                        hotkey=hotkey,
                        docker_image=build.docker_image,
                        current_round=current_round,
                        prompts=prompts,
                        seed=seed,
                        shutdown=shutdown,
                    ),
                    name=f"miner-{hotkey}",
                )

        if asyncio.get_running_loop().time() >= deadline:
            changes.update(_mark_incomplete_as_timed_out(builds))

        if changes:
            await save_builds(git_batcher, current_round, builds)

        if _all_builds_resolved(builds):
            break

        await shutdown.wait(timeout=settings.check_build_interval_seconds)

    if tasks:
        total_miners = len(tasks)
        logger.info(f"Waiting for {total_miners} miners to complete generations")

        completed_miners = 0
        for task in asyncio.as_completed(tasks.values()):
            await task
            completed_miners += 1
            logger.info(f"Progress: {completed_miners}/{total_miners} miners completed")

    async with TargonClient(api_key=settings.targon_api_key.get_secret_value()) as targon:
        await targon.delete_containers_by_prefix(f"miner-{current_round}")


async def _get_miner_build_jobs(git: GitHubClient, run_id: int) -> dict[str, GitHubJob]:
    """Get build jobs keyed by miner hotkey."""
    jobs = await git.get_jobs(run_id)
    return {hotkey: job for job in jobs if (hotkey := _parse_build_job_hotkey(job.name))}


def _parse_build_job_hotkey(job_name: str) -> str | None:
    """Extract hotkey from job name like 'Bake and push images to Google Cloud (HOTKEY)'."""
    prefix = "Bake and push images to Google Cloud ("
    suffix = ")"
    if job_name.startswith(prefix) and job_name.endswith(suffix):
        return job_name[len(prefix) : -len(suffix)]
    return None


def _update_builds(builds: dict[str, BuildInfo], jobs: dict[str, GitHubJob]) -> set[str]:
    """Update builds from jobs, return changed hotkeys."""
    changed = set()

    for hotkey, build in builds.items():
        if build.status in TERMINAL_BUILD_STATUSES:
            continue

        new_status = _get_build_status(jobs.get(hotkey), build)
        if new_status != build.status:
            build.status = new_status
            if new_status == BuildStatus.SUCCESS:
                build.docker_image = _format_docker_image(hotkey, build)
            changed.add(hotkey)

    return changed


def _get_build_status(job: GitHubJob | None, build: BuildInfo) -> BuildStatus:
    """Determine build status from the job state."""
    if job is None:
        return BuildStatus.NOT_FOUND
    if job.conclusion == "success":
        return BuildStatus.SUCCESS
    if job.conclusion in ("failure", "cancelled", "skipped", "timed_out", "action_required"):
        return BuildStatus.FAILURE
    if job.status == "in_progress":
        return BuildStatus.IN_PROGRESS
    return BuildStatus.PENDING


def _format_docker_image(hotkey: str, build: BuildInfo) -> str:
    return settings.docker_image_format.format(
        hotkey10=hotkey[:10],
        tag=build.tag,
    ).lower()


def _mark_incomplete_as_timed_out(results: dict[str, BuildInfo]) -> set[str]:
    changed = set()
    for hotkey, result in results.items():
        if result.status in (BuildStatus.PENDING, BuildStatus.IN_PROGRESS):
            logger.warning(f"Build {hotkey} timed out")
            result.status = BuildStatus.TIMED_OUT
            changed.add(hotkey)
    return changed


def _all_builds_resolved(builds: dict[str, BuildInfo]) -> bool:
    """Check if all builds have reached a terminal state."""
    return all(b.status in TERMINAL_BUILD_STATUSES for b in builds.values())
