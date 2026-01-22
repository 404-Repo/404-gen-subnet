import asyncio
from datetime import datetime

from loguru import logger
from subnet_common.competition.audit_requests import AuditRequest, get_audit_requests
from subnet_common.competition.generation_audit import (
    GenerationAudit,
    GenerationAuditOutcome,
    get_generation_audits,
    save_generation_audits,
)
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.build_tracker import BuildTracker
from generation_orchestrator.generation_stop import GenerationStop, GenerationStopManager
from generation_orchestrator.gpu_provider import GPUProviderManager
from generation_orchestrator.miner_runner import run_miner
from generation_orchestrator.prompts import Prompt, ensure_prompts_cached_from_git
from generation_orchestrator.settings import settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


async def run_generation_iteration(shutdown: GracefulShutdown) -> datetime | None:
    """Run one generation iteration.

    Orchestrates three concurrent tasks:
    - Leader generation: baseline 3D models (~2h, resumable)
    - Build tracking: monitors miner Docker builds
    - Miner generation: processes incoming generation requests

    Returns the next stage ETA if not in generating stages, None otherwise.
    """

    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        ref = await git.get_ref_sha(ref=settings.github_branch)
        state: CompetitionState = await require_state(git=git, ref=ref)
        logger.info(f"Commit: {ref[:10]}, state: {state}")

        if state.stage not in {RoundStage.MINER_GENERATION, RoundStage.DOWNLOADING, RoundStage.DUELS}:
            return state.next_stage_eta

        submissions = await require_submissions(git=git, round_num=state.current_round, ref=ref)
        if not submissions:
            logger.warning("No submissions for round %d", state.current_round)
            return None

        try:
            await _run_generation(
                git=git,
                state=state,
                submissions=submissions,
                shutdown=shutdown,
            )
        finally:
            # Final cleanup of any remaining pods from this round
            if not settings.debug_keep_pods_alive:
                gpu_manager = GPUProviderManager(settings)
                await gpu_manager.cleanup_by_prefix(f"miner-{state.current_round}")

        return None


async def _run_generation(
    git: GitHubClient,
    state: CompetitionState,
    submissions: dict[str, MinerSubmission],
    shutdown: GracefulShutdown,
) -> None:
    """Run the actual generation tasks."""
    ref = await git.get_ref_sha(ref=settings.github_branch)

    seed = await require_seed_from_git(git=git, round_num=state.current_round, ref=ref)
    prompts = await ensure_prompts_cached_from_git(
        git=git,
        cache_dir=settings.cache_dir,
        round_num=state.current_round,
        ref=ref,
    )

    git_batcher = await GitBatcher.create(git=git, branch=settings.github_branch, base_sha=ref)

    build_tracker = BuildTracker(
        git_batcher=git_batcher,
        commit_message=settings.build_commit_message,
        docker_image_format=settings.docker_image_format,
        timeout_seconds=settings.build_timeout_seconds,
        job_poll_interval_seconds=settings.check_build_interval_seconds,
        run_poll_interval_seconds=2 * settings.check_build_interval_seconds,
        shutdown=shutdown,
    )

    semaphore = StaggeredSemaphore(settings.max_concurrent_miners)
    stop_manager = GenerationStopManager()
    gpu_manager = GPUProviderManager(settings)

    # Three concurrent processes + shutdown watcher:
    # - Leader: generates baseline models (~2h, resumable)
    # - Build watcher: tracks miner Docker builds, updates `builds` dict
    # - Miner generator: processes generation requests, skips unready builds
    # - Shutdown watcher: cancels all generation stops
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_on_shutdown(shutdown=shutdown, stop_manager=stop_manager))
        tg.create_task(
            _generate_leader(
                git_batcher=git_batcher,
                gpu_manager=gpu_manager,
                semaphore=semaphore,
                prompts=prompts,
                seed=seed,
                round_num=state.current_round,
                stop=stop_manager.new_stop(),
            )
        )
        tg.create_task(build_tracker.track(round_num=state.current_round, submissions=submissions))
        tg.create_task(
            _generate_for_audits(
                gpu_manager=gpu_manager,
                git_batcher=git_batcher,
                build_tracker=build_tracker,
                semaphore=semaphore,
                prompts=prompts,
                seed=seed,
                round_num=state.current_round,
                shutdown=shutdown,
                stop_manager=stop_manager,
            )
        )

    await git_batcher.flush()


async def _cancel_on_shutdown(shutdown: GracefulShutdown, stop_manager: GenerationStopManager) -> None:
    await shutdown.wait()
    stop_manager.cancel_all("service shutdown")


async def _generate_leader(
    git_batcher: GitBatcher,
    gpu_manager: GPUProviderManager,
    semaphore: StaggeredSemaphore,
    prompts: list[Prompt],
    seed: int,
    round_num: int,
    stop: GenerationStop,
) -> None:
    """Generate baseline 3D models using the current leader's solution.
    Skips prompts that already have results, safe to restart.
    """

    leader_state = await require_leader_state(git=git_batcher.git, ref=git_batcher.base_sha)
    leader = leader_state.get_latest()

    if leader is None:
        raise RuntimeError("Leader docker image not found")

    await run_miner(
        settings=settings,
        semaphore=semaphore,
        gpu_manager=gpu_manager,
        git_batcher=git_batcher,
        hotkey="leader",
        docker_image=leader.docker,
        current_round=round_num,
        prompts=prompts,
        seed=seed,
        stop=stop,
        audit_request=None,  # generation-only mode, no audit
    )


async def _generate_for_audits(
    gpu_manager: GPUProviderManager,
    git_batcher: GitBatcher,
    build_tracker: BuildTracker,
    semaphore: StaggeredSemaphore,
    prompts: list[Prompt],
    seed: int,
    round_num: int,
    shutdown: GracefulShutdown,
    stop_manager: GenerationStopManager,
) -> None:
    """Process audit requests, spawn generation tasks for ready builds."""

    audits = await get_generation_audits(git=git_batcher.git, round_num=round_num, ref=git_batcher.base_sha)

    # Hotkeys we've already started or completed — only grows
    processed: set[str] = {hk for hk, a in audits.items() if a.outcome != GenerationAuditOutcome.PENDING}

    hotkey_stops: dict[str, GenerationStop] = {}
    hotkey_tasks: dict[str, asyncio.Task[GenerationAudit | None]] = {}

    while not shutdown.should_stop:
        await git_batcher.refresh_base_sha()

        state = await require_state(git=git_batcher.git, ref=git_batcher.base_sha)
        if state.stage not in {RoundStage.MINER_GENERATION, RoundStage.DOWNLOADING, RoundStage.DUELS}:
            stop_manager.cancel_all(reason="round stage change")
            break

        # Collect results from completed tasks
        for hotkey in list(hotkey_tasks.keys()):
            task = hotkey_tasks[hotkey]
            if task.done():
                try:
                    audit = task.result()
                    if audit is not None:
                        audits[audit.hotkey] = audit
                        await save_generation_audits(git_batcher=git_batcher, round_num=round_num, audits=audits)
                        await git_batcher.flush()
                except Exception as e:
                    logger.exception(f"{hotkey[:10]}: failed to process audit result with {e}")
                del hotkey_tasks[hotkey]

        # Find new work
        audit_requests = await get_audit_requests(git=git_batcher.git, round_num=round_num, ref=git_batcher.base_sha)
        new_hotkeys = audit_requests.hotkeys - processed

        # TODO: read source audits and cancel generation if needed

        for hotkey in new_hotkeys:
            stop = stop_manager.new_stop()
            hotkey_stops[hotkey] = stop
            processed.add(hotkey)
            audit_request = audit_requests.get(hotkey=hotkey)
            if audit_request is None:
                # Defensive guard: new_hotkeys is derived from audit_requests.hotkeys.
                continue
            hotkey_tasks[hotkey] = asyncio.create_task(
                _generate_for_audit(
                    semaphore=semaphore,
                    gpu_manager=gpu_manager,
                    git_batcher=git_batcher,
                    hotkey=hotkey,
                    audit_request=audit_request,
                    build_tracker=build_tracker,
                    prompts=prompts,
                    seed=seed,
                    round_num=round_num,
                    stop=stop,
                )
            )

        await shutdown.wait(timeout=settings.check_audit_interval_seconds)

    # Wait for remaining tasks and collect their results
    for task in hotkey_tasks.values():
        await task


async def _generate_for_audit(
    semaphore: StaggeredSemaphore,
    gpu_manager: GPUProviderManager,
    git_batcher: GitBatcher,
    hotkey: str,
    audit_request: AuditRequest,
    build_tracker: BuildTracker,
    prompts: list[Prompt],
    seed: int,
    round_num: int,
    stop: GenerationStop,
) -> GenerationAudit | None:
    """Generate for a single audit, return an audit result."""
    build = await build_tracker.wait_for_build(hotkey=hotkey, generation_stop=stop)

    if stop.should_stop:
        return None

    docker = build.get_runnable_image() if build else None
    if docker is None:
        return GenerationAudit(
            hotkey=hotkey,
            outcome=GenerationAuditOutcome.REJECTED,
            reason="Failed to build a docker image",
        )

    return await run_miner(
        settings=settings,
        semaphore=semaphore,
        gpu_manager=gpu_manager,
        git_batcher=git_batcher,
        hotkey=hotkey,
        docker_image=docker,
        current_round=round_num,
        prompts=prompts,
        seed=seed,
        stop=stop,
        audit_request=audit_request,
    )
