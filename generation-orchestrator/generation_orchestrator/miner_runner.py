import asyncio
from enum import Enum

from loguru import logger
from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_audit import GenerationAudit, GenerationAuditOutcome
from subnet_common.competition.generations import GenerationResult, GenerationSource, get_generations
from subnet_common.git_batcher import GitBatcher

from generation_orchestrator.failure_tracker import FailureTracker
from generation_orchestrator.generation_pipeline import GenerationPipeline
from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.gpu_provider import DeployedContainer, GPUProviderManager
from generation_orchestrator.miner_audit import audit_generations
from generation_orchestrator.prompt_queue import PromptQueue, PromptTask
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

        self._queue = PromptQueue(log_id=self._log_id, max_attempts=settings.max_prompt_attempts)
        self._generations: dict[str, GenerationResult] = {}
        self._submitted: dict[str, GenerationResult] = {}

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

        self._generations = await get_generations(
            git=self._git_batcher.git,
            hotkey=self._hotkey,
            round_num=self._current_round,
            source=GenerationSource.GENERATED,
            ref=self._settings.github_branch,
        )

        tasks = self._build_tasks()
        if not tasks:
            if self._audit_request is not None:
                logger.info(f"{self._log_id}: no prompts to process ({len(self._submitted)} valid submissions)")
            else:
                logger.info(f"{self._log_id}: no prompts to process (all already generated)")
            return self._build_audit(_Verdict.COMPLETED)

        self._queue.fill(tasks)
        logger.info(f"{self._log_id}: {len(self._queue)} prompts to process")

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

    def _build_tasks(self) -> list[PromptTask]:
        """Build task list from prompts."""
        tasks: list[PromptTask] = []

        for prompt in self._prompts:
            submitted_png: str | None = None

            if self._audit_request is not None:
                sub = self._submitted.get(prompt.stem)
                if sub is None:
                    continue
                submitted_png = sub.png

            existing = self._generations.get(prompt.stem)
            if existing is None:
                tasks.append(PromptTask(prompt=prompt, submitted_png=submitted_png, attempts=0))
            elif existing.needs_retry(
                timeout=self._settings.generation_median_limit_seconds,
                max_attempts=self._settings.max_prompt_attempts,
            ):
                tasks.append(PromptTask(prompt=prompt, submitted_png=submitted_png, attempts=existing.attempts))

        return tasks

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
            generations=self._generations,
            settings=self._settings,
        )

    def _should_start_pod(self) -> bool:
        """Check if conditions allow starting a new pod."""
        return (
            not self._stop.should_stop
            and self._pods_started < self._settings.max_pod_attempts
            and not self._queue.empty()
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

        worker_id = f"{self._miner_prefix}-{idx}"

        task = asyncio.create_task(
            self._worker_task(worker_id=worker_id, provider_start_index=idx),
            name=worker_id,
        )
        self._workers[worker_id] = task

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

            if self._queue.empty() and self._workers:
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
            worker_id = task.get_name()
            self._workers.pop(worker_id, None)
            try:
                task.result()
                logger.debug(f"{self._log_id}: worker {worker_id} finished")
            except Exception as e:
                logger.exception(f"{self._log_id}: worker {worker_id} crashed with {e}")

    def _determine_verdict(self) -> _Verdict:
        """Determine a final verdict based on queue and stop state."""
        if self._queue.empty():
            return _Verdict.COMPLETED
        if self._stop.should_stop:
            return _Verdict.PARTIAL
        return _Verdict.EARLY_STOP

    async def _worker_task(self, worker_id: str, provider_start_index: int) -> None:
        """Worker lifecycle: deploy pod, process prompts, cleanup."""
        deployed = await self._deploy_and_wait_healthy(worker_id, provider_start_index)
        if deployed is None:
            return

        self._first_healthy.set()

        try:
            tracker = FailureTracker(
                min_samples=self._settings.pod_min_samples,
                failure_threshold=self._settings.pod_failure_threshold,
                median_limit=self._settings.generation_median_limit_seconds,
                hard_limit=self._settings.generation_timeout_seconds,
                acceptable_distance=self._settings.acceptable_distance,
            )

            pipeline = GenerationPipeline(
                settings=self._settings,
                pod_endpoint=deployed.info.url,
                generation_token=deployed.generation_token,
                queue=self._queue,
                generations=self._generations,
                git_batcher=self._git_batcher,
                hotkey=self._hotkey,
                current_round=self._current_round,
                seed=self._seed,
                stop=self._stop,
            )

            await pipeline.run(worker_id=worker_id, tracker=tracker)
        finally:
            if not self._settings.debug_keep_pods_alive:
                await self._gpu_manager.delete_container(deployed.info)

    async def _deploy_and_wait_healthy(
        self,
        worker_id: str,
        provider_start_index: int,
    ) -> DeployedContainer | None:
        """Deploy a pod and wait for it to become healthy."""
        logger.debug(f"{worker_id}: deploying with provider_start_index={provider_start_index}")

        deployed = await self._gpu_manager.get_healthy_pod(
            name=worker_id,
            image=self._docker_image,
            gpu_type="H200",
            stop=self._stop,
            start_index=provider_start_index,
        )

        if deployed is None:
            if not self._stop.should_stop:
                logger.warning(f"{worker_id}: failed to get healthy pod")
            return None

        logger.info(f"{worker_id}: pod healthy at {deployed.info.url}")
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
