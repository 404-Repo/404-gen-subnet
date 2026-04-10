import asyncio
from datetime import datetime

from loguru import logger
from subnet_common.competition.audit_requests import AuditRequest, AuditRequests, get_audit_requests
from subnet_common.competition.generation_report import (
    GenerationReport,
    GenerationReportOutcome,
    get_generation_reports,
    save_generation_reports,
)
from subnet_common.competition.leader import require_leader_state
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import CompetitionState, RoundStage, require_state
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.build_tracker import BuildTracker
from generation_orchestrator.discord import NULL_DISCORD_NOTIFIER, DiscordNotifier
from generation_orchestrator.generation_stop import GenerationStop, GenerationStopManager
from generation_orchestrator.gpu_provider import GPUProviderManager
from generation_orchestrator.miner_runner import MinerRunner
from generation_orchestrator.prompts import Prompt, ensure_prompts_cached_from_git
from generation_orchestrator.settings import Settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


GENERATING_STAGES = {RoundStage.MINER_GENERATION, RoundStage.DOWNLOADING, RoundStage.DUELS}


async def run_generation_iteration(
    settings: Settings, shutdown: GracefulShutdown, discord: DiscordNotifier = NULL_DISCORD_NOTIFIER
) -> datetime | None:
    """Run one generation iteration.

    Orchestrates four concurrent tasks:
    - Leader generation: baseline 3D models (~2h, resumable)
    - Build tracking: monitors miner Docker builds
    - Miner generation: processes incoming generation requests
    - Shutdown watcher: cancels all generation stops on shutdown or stage change

    Returns the next stage ETA if not in generating stages, None otherwise.
    """

    async with GitHubClient(
        repo=settings.github_repo,
        token=settings.github_token.get_secret_value(),
    ) as git:
        ref = await git.get_ref_sha(ref=settings.github_branch)
        state: CompetitionState = await require_state(git=git, ref=ref)
        logger.info(f"Commit: {ref[:10]}, state: {state}")

        if state.stage not in GENERATING_STAGES:
            return state.next_stage_eta

        submissions = await require_submissions(git=git, round_num=state.current_round, ref=ref)
        if not submissions:
            logger.warning(f"No submissions for round {state.current_round}")
            return None

        gpu_manager = GPUProviderManager(settings)
        try:
            await _run_generation(
                settings=settings,
                git=git,
                gpu_manager=gpu_manager,
                state=state,
                submissions=submissions,
                shutdown=shutdown,
                discord=discord,
            )
        finally:
            await gpu_manager.cleanup_by_prefix(f"miner-{state.current_round}")

        return None


async def _run_generation(
    settings: Settings,
    git: GitHubClient,
    gpu_manager: GPUProviderManager,
    state: CompetitionState,
    submissions: dict[str, MinerSubmission],
    shutdown: GracefulShutdown,
    discord: DiscordNotifier = NULL_DISCORD_NOTIFIER,
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

    # Four concurrent tasks:
    # - Shutdown watcher: cancels all generation stops on shutdown or stage change
    # - Leader: generates baseline models (~2h, resumable)
    # - Build watcher: tracks miner Docker builds, updates `builds` dict
    # - Miner generator: processes generation requests, skips unready builds
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_on_shutdown(shutdown=shutdown, stop_manager=stop_manager))
        tg.create_task(
            _generate_leader(
                settings=settings,
                git_batcher=git_batcher,
                gpu_manager=gpu_manager,
                semaphore=semaphore,
                prompts=prompts,
                seed=seed,
                round_num=state.current_round,
                stop=stop_manager.new_stop(),
                discord=discord,
            )
        )
        tg.create_task(build_tracker.track(round_num=state.current_round, submissions=submissions))
        tg.create_task(
            _process_audit_requests(
                settings=settings,
                gpu_manager=gpu_manager,
                git_batcher=git_batcher,
                build_tracker=build_tracker,
                semaphore=semaphore,
                prompts=prompts,
                seed=seed,
                round_num=state.current_round,
                shutdown=shutdown,
                stop_manager=stop_manager,
                discord=discord,
            )
        )

    await git_batcher.flush()


async def _cancel_on_shutdown(shutdown: GracefulShutdown, stop_manager: GenerationStopManager) -> None:
    """Wait for shutdown or stage change; cancel all generation stops on shutdown."""
    stop = stop_manager.new_stop()
    tasks = [asyncio.create_task(shutdown.wait()), asyncio.create_task(stop.wait())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
    if shutdown.should_stop:
        stop_manager.cancel_all("service shutdown")


async def _generate_leader(
    settings: Settings,
    git_batcher: GitBatcher,
    gpu_manager: GPUProviderManager,
    semaphore: StaggeredSemaphore,
    prompts: list[Prompt],
    seed: int,
    round_num: int,
    stop: GenerationStop,
    discord: DiscordNotifier = NULL_DISCORD_NOTIFIER,
) -> None:
    """Generate baseline 3D models using the current leader's solution.
    Skips prompts that already have results, safe to restart.
    """

    leader_state = await require_leader_state(git=git_batcher.git, ref=git_batcher.base_sha)
    leader = leader_state.get_latest()

    if leader is None:
        raise RuntimeError("Leader docker image not found")

    await MinerRunner(
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
        audit_request=None,
        discord=discord,
    ).run()
    await git_batcher.flush()


async def _process_audit_requests(
    settings: Settings,
    gpu_manager: GPUProviderManager,
    git_batcher: GitBatcher,
    build_tracker: BuildTracker,
    semaphore: StaggeredSemaphore,
    prompts: list[Prompt],
    seed: int,
    round_num: int,
    shutdown: GracefulShutdown,
    stop_manager: GenerationStopManager,
    discord: DiscordNotifier = NULL_DISCORD_NOTIFIER,
) -> None:
    """Poll for audit requests, spawn per-miner generation tasks, collect reports."""

    reports = await get_generation_reports(git=git_batcher.git, round_num=round_num, ref=git_batcher.base_sha)
    processed: set[str] = {hk for hk, r in reports.items() if r.outcome != GenerationReportOutcome.PENDING}
    hotkey_tasks: dict[str, asyncio.Task[GenerationReport | None]] = {}

    async with asyncio.TaskGroup() as tg:
        while not shutdown.should_stop:
            try:
                await git_batcher.refresh_base_sha()
                state = await require_state(git=git_batcher.git, ref=git_batcher.base_sha)
                audit_requests = await get_audit_requests(
                    git=git_batcher.git, round_num=round_num, ref=git_batcher.base_sha
                )
                # TODO: read source audits and cancel generation if needed
            except Exception as e:
                logger.warning(f"Failed to refresh state: {e}")
                await shutdown.wait(timeout=settings.check_audit_interval_seconds)
                continue

            if state.stage not in GENERATING_STAGES:
                stop_manager.cancel_all(reason="round stage change")
                break

            await _collect_finished(
                hotkey_tasks=hotkey_tasks,
                reports=reports,
                git_batcher=git_batcher,
                round_num=round_num,
                discord=discord,
            )
            _spawn_new(
                tg=tg,
                audit_requests=audit_requests,
                processed=processed,
                hotkey_tasks=hotkey_tasks,
                stop_manager=stop_manager,
                settings=settings,
                semaphore=semaphore,
                gpu_manager=gpu_manager,
                git_batcher=git_batcher,
                build_tracker=build_tracker,
                prompts=prompts,
                seed=seed,
                round_num=round_num,
                discord=discord,
            )

            await shutdown.wait(timeout=settings.check_audit_interval_seconds)


async def _collect_finished(
    hotkey_tasks: dict[str, asyncio.Task[GenerationReport | None]],
    reports: dict[str, GenerationReport],
    git_batcher: GitBatcher,
    round_num: int,
    discord: DiscordNotifier,
) -> None:
    """Process any finished tasks: save their report and notify."""
    for hotkey in list(hotkey_tasks.keys()):
        task = hotkey_tasks[hotkey]
        if not task.done():
            continue
        del hotkey_tasks[hotkey]
        if task.cancelled():
            continue
        report = task.result()
        if report is None:
            continue
        try:
            reports[report.hotkey] = report
            await save_generation_reports(git_batcher=git_batcher, round_num=round_num, reports=reports)
            await git_batcher.flush()
            await discord.notify_generation_report(report)
        except Exception as e:
            logger.exception(f"{hotkey[:10]}: failed to persist generation report with {e}")


def _spawn_new(
    tg: asyncio.TaskGroup,
    audit_requests: AuditRequests,
    processed: set[str],
    hotkey_tasks: dict[str, asyncio.Task[GenerationReport | None]],
    stop_manager: GenerationStopManager,
    settings: Settings,
    semaphore: StaggeredSemaphore,
    gpu_manager: GPUProviderManager,
    git_batcher: GitBatcher,
    build_tracker: BuildTracker,
    prompts: list[Prompt],
    seed: int,
    round_num: int,
    discord: DiscordNotifier,
) -> None:
    """Spawn generation tasks for any audit requests not yet seen."""
    for hotkey in audit_requests.hotkeys - processed:
        audit_request = audit_requests.get(hotkey=hotkey)
        if audit_request is None:
            continue
        processed.add(hotkey)
        hotkey_tasks[hotkey] = tg.create_task(
            _generate_report(
                settings=settings,
                semaphore=semaphore,
                gpu_manager=gpu_manager,
                git_batcher=git_batcher,
                hotkey=hotkey,
                audit_request=audit_request,
                build_tracker=build_tracker,
                prompts=prompts,
                seed=seed,
                round_num=round_num,
                stop=stop_manager.new_stop(),
                discord=discord,
            )
        )


async def _generate_report(
    settings: Settings,
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
    discord: DiscordNotifier = NULL_DISCORD_NOTIFIER,
) -> GenerationReport | None:
    """Generate for a single audit request, return a generation report.

    Swallows unexpected exceptions so sibling tasks in the TaskGroup are not cancelled.
    """
    try:
        build = await build_tracker.wait_for_build(hotkey=hotkey, generation_stop=stop)

        if stop.should_stop:
            return None

        docker = build.get_runnable_image() if build else None
        if docker is None:
            return GenerationReport(
                hotkey=hotkey,
                outcome=GenerationReportOutcome.REJECTED,
                reason="Failed to build a docker image",
            )

        return await MinerRunner(
            settings=settings,
            semaphore=semaphore,
            discord=discord,
            gpu_manager=gpu_manager,
            git_batcher=git_batcher,
            hotkey=hotkey,
            docker_image=docker,
            current_round=round_num,
            prompts=prompts,
            seed=seed,
            stop=stop,
            audit_request=audit_request,
        ).run()
    except Exception as e:
        logger.exception(f"{hotkey[:10]}: generation failed with {e}")
        return None
