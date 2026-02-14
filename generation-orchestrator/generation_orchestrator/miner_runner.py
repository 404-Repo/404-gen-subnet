import asyncio
from enum import Enum

from loguru import logger
from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_audit import GenerationAudit, GenerationAuditOutcome
from subnet_common.competition.generations import GenerationResult, GenerationSource, get_generations
from subnet_common.competition.pod_stats import PodStats, save_pod_stats
from subnet_common.git_batcher import GitBatcher

from generation_orchestrator.generation_pipeline import GenerationPipeline
from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.gpu_provider import DeployedContainer, GPUProviderManager
from generation_orchestrator.miner_audit import audit_generations
from generation_orchestrator.pod_tracker import PodTracker
from generation_orchestrator.prompt_coordinator import PromptCoordinator, PromptEntry
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.settings import Settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


class _Verdict(Enum):
    """Internal outcome of a miner run."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    DEPLOY_FAILURE = "deploy_failure"
    EARLY_STOP = "early_stop"


class MinerRunner:
    """
    Orchestrates pod lifecycle and prompt processing for a single miner.

    Worker = Pod. Each worker task owns its pod's entire lifecycle:
    deploy → health check → process prompts → cleanup.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        semaphore: StaggeredSemaphore,
        gpu_manager: GPUProviderManager,
        git_batcher: GitBatcher,
        hotkey: str,
        docker_image: str,
        prompts: list[Prompt],
        current_round: int,
        seed: int,
        stop: GenerationStop,
        audit_request: AuditRequest | None = None,
    ):
        self._settings = settings
        self._semaphore = semaphore
        self._gpu_manager = gpu_manager
        self._git_batcher = git_batcher
        self._hotkey = hotkey
        self._docker_image = docker_image
        self._prompts = prompts
        self._current_round = current_round
        self._seed = seed
        self._stop = stop
        self._audit_request = audit_request

        self._log_id = hotkey[:10]
        self._miner_prefix = f"miner-{self._current_round:02d}-{self._hotkey[:10].lower()}"

        self._pods_started = 0
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._first_healthy = asyncio.Event()

        self._coordinator = PromptCoordinator(
            max_attempts=settings.max_prompt_attempts,
            lock_timeout=settings.prompt_lock_timeout_seconds,
            acceptable_distance=settings.acceptable_distance,
            median_limit=settings.generation_median_limit_seconds,
            hard_limit=settings.generation_timeout_seconds,
            median_trim_count=settings.generation_median_trim_count,
        )
        self._submitted: dict[str, GenerationResult] = {}
        self._pod_stats: dict[str, PodStats] = {}

    async def run(self) -> GenerationAudit | None:
        """
        Run generation for one miner.

        Returns:
            GenerationAudit - generation completed (passed or rejected)
            None - generation-only run, or interrupted (should retry later)
        """
        async with self._semaphore:
            if self._stop.should_stop:
                return None
            return await self._run()

    async def _run(self) -> GenerationAudit | None:
        """Load state, build task queue, orchestrate pods, return audit result."""
        if self._audit_request is not None:
            all_submitted = await get_generations(
                git=self._git_batcher.git,
                hotkey=self._hotkey,
                round_num=self._current_round,
                source=GenerationSource.SUBMITTED,
                ref=self._settings.github_branch,
            )
            self._submitted = {k: v for k, v in all_submitted.items() if not v.is_failed()}

        generations = await get_generations(
            git=self._git_batcher.git,
            hotkey=self._hotkey,
            round_num=self._current_round,
            source=GenerationSource.GENERATED,
            ref=self._settings.github_branch,
        )

        if self._audit_request is not None:
            entries = self._audit_entries()
        else:
            entries = [PromptEntry(prompt=p) for p in self._prompts]

        self._coordinator.seed(entries, generations)
        if self._coordinator.all_done():
            logger.info(f"{self._log_id}: all {len(self._coordinator)} prompts already done")
            return self._build_audit(_Verdict.COMPLETED)

        logger.info(f"{self._log_id}: {len(self._coordinator)} prompts to process")

        try:
            await self._expand_pods(target=self._settings.initial_pod_count)
            await self._wait_for_first_healthy_or_all_failed()

            if self._stop.should_stop:
                logger.info(f"{self._log_id}: stopped during initial pod warmup")
                return None

            if not self._first_healthy.is_set():
                logger.error(f"{self._log_id}: all {self._settings.initial_pod_count} initial pods failed warmup")
                return self._build_audit(_Verdict.DEPLOY_FAILURE)

            await self._expand_pods(target=self._settings.pods_per_miner)
            await self._run_until_complete()

            verdict = self._determine_verdict()
            logger.info(f"{self._log_id}: finished with verdict={verdict.value}")
            return self._build_audit(verdict)
        finally:
            await self._cleanup_all()

    def _audit_entries(self) -> list[PromptEntry]:
        """Build entries for audit mode: only prompts with valid submissions."""
        entries: list[PromptEntry] = []
        for prompt in self._prompts:
            sub = self._submitted.get(prompt.stem)
            if sub is not None:
                entries.append(PromptEntry(prompt=prompt, submitted_png=sub.png))
        return entries

    def _build_audit(self, verdict: _Verdict) -> GenerationAudit | None:
        """Build audit result based on the verdict."""
        if self._audit_request is None:
            return None

        if verdict == _Verdict.PARTIAL:
            return None

        if verdict == _Verdict.DEPLOY_FAILURE:
            return GenerationAudit(
                hotkey=self._hotkey,
                outcome=GenerationAuditOutcome.REJECTED,
                reason="Failed to deploy a docker image",
            )

        return audit_generations(
            hotkey=self._hotkey,
            audit_request=self._audit_request,
            submitted=self._submitted,
            generations=self._coordinator.generations,
            settings=self._settings,
        )

    def _should_start_pod(self) -> bool:
        """Check if conditions allow starting a new pod."""
        return (
            not self._stop.should_stop
            and self._pods_started < self._settings.max_pod_attempts
            and not self._coordinator.all_done()
        )

    async def _expand_pods(self, target: int) -> None:
        """Start additional workers to reach the target pod count."""
        current = len(self._workers)
        needed = target - current
        if needed <= 0:
            return

        started = 0
        while started < needed and self._should_start_pod():
            if started > 0:
                await asyncio.sleep(self._settings.pod_start_delay_seconds)
            self._start_worker()
            started += 1

        if started > 0:
            logger.info(f"{self._log_id}: expanded from {current} to {current + started} workers")

    def _start_worker(self) -> None:
        """Create and register a new worker task."""
        idx = self._pods_started
        self._pods_started += 1

        pod_id = f"{self._miner_prefix}-{idx}"

        task = asyncio.create_task(
            self._worker_task(pod_id=pod_id, provider_start_index=idx),
            name=pod_id,
        )
        self._workers[pod_id] = task

    async def _wait_for_first_healthy_or_all_failed(self) -> None:
        """Block until at least one pod is healthy or all workers have exited."""
        if not self._workers:
            return

        async def all_done() -> None:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)

        event_waiter = asyncio.create_task(self._first_healthy.wait())
        all_workers = asyncio.create_task(all_done())

        try:
            await asyncio.wait({event_waiter, all_workers}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            event_waiter.cancel()

    async def _run_until_complete(self) -> None:
        """Process workers until queue empty or stopped."""
        while self._workers and not self._stop.should_stop:
            done, _ = await asyncio.wait(self._workers.values(), return_when=asyncio.FIRST_COMPLETED)
            self._reap_workers(done)

            if self._coordinator.all_done() and self._workers:
                self._stop.cancel("no_tasks")
                break

            await self._expand_pods(target=self._settings.pods_per_miner)

        if self._workers:
            logger.info(f"{self._log_id}: waiting for {len(self._workers)} workers to finish")
            done, _ = await asyncio.wait(self._workers.values(), return_when=asyncio.ALL_COMPLETED)
            self._reap_workers(done)

    def _reap_workers(self, done: set[asyncio.Task]) -> None:
        """Remove completed workers and log any errors."""
        for task in done:
            pod_id = task.get_name()
            self._workers.pop(pod_id, None)
            try:
                task.result()
                logger.debug(f"{self._log_id}: worker {pod_id} finished")
            except Exception as e:
                logger.exception(f"{self._log_id}: worker {pod_id} crashed with {e}")

    def _determine_verdict(self) -> _Verdict:
        """Determine a final verdict based on queue and stop state."""
        if self._coordinator.all_done():
            return _Verdict.COMPLETED
        if self._stop.should_stop:
            return _Verdict.PARTIAL
        return _Verdict.EARLY_STOP

    async def _worker_task(self, pod_id: str, provider_start_index: int) -> None:
        """Worker lifecycle: deploy pod, process prompts, cleanup."""
        deployed = await self._deploy_and_wait_healthy(pod_id, provider_start_index)
        if deployed is None:
            return

        self._first_healthy.set()

        try:
            tracker = PodTracker(
                min_samples=self._settings.pod_min_samples,
                failure_threshold=self._settings.pod_failure_threshold,
                warmup_half_window=self._settings.warmup_half_window,
                rolling_median_limit=self._settings.rolling_median_limit_seconds,
                hard_limit=self._settings.generation_timeout_seconds,
                retry_delta_min_samples=self._settings.retry_delta_min_samples,
            )

            pipeline = GenerationPipeline(
                settings=self._settings,
                pod_endpoint=deployed.info.url,
                generation_token=deployed.generation_token,
                coordinator=self._coordinator,
                git_batcher=self._git_batcher,
                hotkey=self._hotkey,
                current_round=self._current_round,
                seed=self._seed,
                stop=self._stop,
            )

            await pipeline.run(pod_id=pod_id, tracker=tracker)
        finally:
            self._pod_stats[pod_id] = tracker.to_stats(pod_id=pod_id)
            await save_pod_stats(
                git_batcher=self._git_batcher,
                round_num=self._current_round,
                hotkey=self._hotkey,
                stats=self._pod_stats,
            )
            if not self._settings.debug_keep_pods_alive:
                await self._gpu_manager.delete_container(deployed.info)

    async def _deploy_and_wait_healthy(
        self,
        pod_id: str,
        provider_start_index: int,
    ) -> DeployedContainer | None:
        """Deploy a pod and wait for it to become healthy."""
        logger.debug(f"{pod_id}: deploying with provider_start_index={provider_start_index}")

        deployed = await self._gpu_manager.get_healthy_pod(
            name=pod_id,
            image=self._docker_image,
            gpu_type="H200",
            stop=self._stop,
            start_index=provider_start_index,
        )

        if deployed is None:
            if not self._stop.should_stop:
                logger.warning(f"{pod_id}: failed to get healthy pod")
            return None

        logger.info(f"{pod_id}: pod healthy at {deployed.info.url}")
        return deployed

    async def _cleanup_all(self) -> None:
        """Signal stop and wait for all workers to finish."""
        if not self._stop.should_stop:
            self._stop.cancel("cleanup")

        if self._workers:
            logger.info(f"{self._log_id}: cleaning up {len(self._workers)} workers")
            await self._gpu_manager.cleanup_by_prefix(self._miner_prefix)
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
            self._workers.clear()


async def run_miner(
    *,
    settings: Settings,
    semaphore: StaggeredSemaphore,
    gpu_manager: GPUProviderManager,
    git_batcher: GitBatcher,
    hotkey: str,
    docker_image: str,
    prompts: list[Prompt],
    current_round: int,
    seed: int,
    stop: GenerationStop,
    audit_request: AuditRequest | None = None,
) -> GenerationAudit | None:
    """
    Run generation for a single miner.

    Returns GenerationAudit on completion, None for generation-only runs or interruptions.
    """
    runner = MinerRunner(
        settings=settings,
        semaphore=semaphore,
        gpu_manager=gpu_manager,
        git_batcher=git_batcher,
        hotkey=hotkey,
        docker_image=docker_image,
        prompts=prompts,
        current_round=current_round,
        seed=seed,
        stop=stop,
        audit_request=audit_request,
    )
    return await runner.run()
