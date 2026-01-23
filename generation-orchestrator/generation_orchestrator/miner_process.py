import asyncio

import aiofiles
from loguru import logger
from pydantic import BaseModel
from subnet_common.competition.generations import GenerationResult, get_miner_generations, save_miner_generations
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.generate import generate
from generation_orchestrator.gpu_provider import (
    GPUProviderError,
    GPUProviderManager,
)
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.r2_client import R2Client
from generation_orchestrator.render import render
from generation_orchestrator.settings import settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


WARMUP_FAILURE_COST = 2  # Pod acquisition/health check failures are expensive
RUNTIME_FAILURE_COST = 1  # Overtime, GPU loss, etc.


class FailureTracker:
    """Tracks prompt processing results and budget for a single pod attempt."""

    def __init__(self, total_prompts: int):
        self.total = total_prompts
        self.completed = 0
        self.successful = 0
        self.failed = 0
        self.overtime = 0

    @property
    def budget_exceeded(self) -> bool:
        """Check if the fail budget has been exceeded."""
        total_failures = self.failed + self.overtime
        return total_failures >= settings.max_failed_prompts_budget

    def record_result(self, success: bool, is_overtime: bool) -> None:
        """Record a prompt result."""
        self.completed += 1

        if not success:
            self.failed += 1
        elif is_overtime:
            self.overtime += 1
        else:
            self.successful += 1

    def should_retry(self) -> tuple[bool, str]:
        """Decide if we should retry with a new pod based on this attempt's results."""

        if self.failed == 0 and self.overtime == 0:
            return False, "all prompts successful"

        total_failures = self.failed + self.overtime
        if total_failures >= settings.max_failed_prompts_budget:
            return True, f"pod broken: {self.failed} failed, {self.overtime} overtime"

        return False, f"pod ok: {self.failed} failed, {self.overtime} overtime"

    def progress_str(self) -> str:
        """Get a progress string for this attempt."""
        return (
            f"{self.successful} ok, {self.failed} failed, {self.overtime} overtime "
            f"(processed {self.completed}/{self.total})"
        )


class Miner(BaseModel):
    hotkey: str
    docker_image: str
    generations: dict[str, GenerationResult]

    @property
    def log_id(self) -> str:
        """Short ID for logging."""
        return self.hotkey[:10]

    def remaining_prompts(self, prompts: list[Prompt]) -> list[Prompt]:
        """Get prompts that still need processing."""
        timeout = settings.generation_timeout_seconds
        return [p for p in prompts if p.name not in self.generations or self.generations[p.name].needs_retry(timeout)]

    @classmethod
    async def load(cls, git_batcher: GitBatcher, hotkey: str, docker_image: str, current_round: int) -> "Miner":
        """Load miner from git."""
        generations = await get_miner_generations(
            git=git_batcher.git, hotkey=hotkey, round_num=current_round, ref=settings.github_branch
        )
        return cls(hotkey=hotkey, docker_image=docker_image, generations=generations)

    async def save(self, git_batcher: GitBatcher, current_round: int) -> None:
        """Save generations to git."""
        await save_miner_generations(
            git_batcher=git_batcher, hotkey=self.hotkey, round_num=current_round, generations=self.generations
        )


async def process_miner_with_retries(
    semaphore: StaggeredSemaphore,
    git_batcher: GitBatcher,
    hotkey: str,
    docker_image: str,
    current_round: int,
    prompts: list[Prompt],
    seed: int,
    shutdown: GracefulShutdown,
) -> None:
    """Process a miner's prompts with retries.

    Attempt budget system:
    - Total budget: settings.miner_process_attempts (e.g., 4)
    - Warmup failure (pod didn't become healthy): costs 2
    - Runtime failure (overtime, GPU issues): costs 1

    This means: max 2 warmup failures OR max 4 runtime failures.
    """
    gpu_manager = GPUProviderManager(settings)
    budget = settings.miner_process_attempts
    attempt = 0

    while budget > 0 and not shutdown.should_stop:
        attempt += 1
        async with semaphore:
            try:
                miner = await Miner.load(git_batcher, hotkey, docker_image, current_round)

                remaining = miner.remaining_prompts(prompts)
                if not remaining:
                    logger.info(f"{miner.log_id}: all {len(prompts)} prompts already completed")
                    return

                logger.info(
                    f"{miner.log_id}: attempt {attempt} (budget: {budget}), processing {len(remaining)} prompts"
                )

                pod_ok, tracker = await _process_with_pod(
                    gpu_manager=gpu_manager,
                    git_batcher=git_batcher,
                    miner=miner,
                    prompts=remaining,
                    current_round=current_round,
                    seed=seed,
                    shutdown=shutdown,
                )

                if not pod_ok or tracker is None:
                    budget -= WARMUP_FAILURE_COST
                    logger.warning(f"{miner.log_id}: warmup failure (budget: {budget})")
                    continue

                logger.info(f"{miner.log_id}: {tracker.progress_str()}")

                should_retry, reason = tracker.should_retry()
                logger.info(f"{miner.log_id}: {reason}")

                if not should_retry:
                    return

                budget -= RUNTIME_FAILURE_COST
                logger.info(f"{miner.log_id}: will retry with new pod (budget: {budget})")

            except Exception as e:
                budget -= RUNTIME_FAILURE_COST
                logger.exception(f"{hotkey[:10]}: attempt {attempt} failed (budget: {budget}): {e}")

    logger.error(f"{hotkey[:10]}: exhausted budget after {attempt} attempts")


async def _process_with_pod(
    gpu_manager: GPUProviderManager,
    git_batcher: GitBatcher,
    miner: Miner,
    prompts: list[Prompt],
    current_round: int,
    seed: int,
    shutdown: GracefulShutdown,
) -> tuple[bool, FailureTracker | None]:
    """Process prompts with a pod. Returns (pod_started, tracker) or (False, None)."""
    container_name = f"miner-{current_round}-{miner.log_id.lower()}"

    deployed = await gpu_manager.get_healthy_pod(
        name=container_name,
        image=miner.docker_image,
        gpu_type="H200",
        shutdown=shutdown,
    )

    if deployed is None:
        return False, None

    try:
        tracker = await _process_prompts(
            git_batcher=git_batcher,
            miner=miner,
            endpoint=deployed.info.url,
            generation_token=deployed.generation_token,
            prompts=prompts,
            current_round=current_round,
            seed=seed,
            shutdown=shutdown,
        )
        if tracker.budget_exceeded:
            logger.warning(f"{miner.log_id}: stopped early, budget exceeded")
        return True, tracker
    finally:
        if not settings.debug_keep_pods_alive:
            try:
                await gpu_manager.delete_container(deployed.info)
            except GPUProviderError as e:
                logger.warning(f"{miner.log_id}: failed to delete container: {e}")


async def _process_prompts(
    git_batcher: GitBatcher,
    miner: Miner,
    endpoint: str,
    generation_token: str | None,
    prompts: list[Prompt],
    current_round: int,
    seed: int,
    shutdown: GracefulShutdown,
) -> FailureTracker:
    """Process prompts for a miner with early stopping on budget exceeded."""

    request_sem = asyncio.Semaphore(1)  # Using semaphores to limit request to one at a time.
    process_sem = asyncio.Semaphore(settings.max_concurrent_prompts_per_miner)  # Limiting request to control traffic
    tracker = FailureTracker(len(prompts))

    async with R2Client(
        access_key_id=settings.r2_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        r2_endpoint=settings.r2_endpoint.get_secret_value(),
    ) as r2:
        tasks = [
            asyncio.create_task(
                _process_prompt(
                    git_batcher=git_batcher,
                    miner=miner,
                    endpoint=endpoint,
                    generation_token=generation_token,
                    prompt=prompt,
                    current_round=current_round,
                    seed=seed,
                    request_sem=request_sem,
                    process_sem=process_sem,
                    r2=r2,
                    shutdown=shutdown,
                    tracker=tracker,
                )
            )
            for prompt in prompts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for prompt, result in zip(prompts, results, strict=True):
            if isinstance(result, Exception):
                logger.error(f"{miner.hotkey[:10]}/{prompt.name}: {result}")

    return tracker


async def _process_prompt(
    git_batcher: GitBatcher,
    miner: Miner,
    endpoint: str,
    generation_token: str | None,
    prompt: Prompt,
    current_round: int,
    seed: int,
    request_sem: asyncio.Semaphore,
    process_sem: asyncio.Semaphore,
    r2: R2Client,
    shutdown: GracefulShutdown,
    tracker: FailureTracker,
) -> None:
    """Process a single prompt with retries."""
    log_id = f"{miner.log_id}/{prompt.name}"

    async with process_sem:
        if shutdown.should_stop:
            return

        if tracker.budget_exceeded:
            return

        async with aiofiles.open(prompt.path, "rb") as f:
            image = await f.read()

        ply_content, png_content, gen_time = await _generate_and_render_with_retries(
            endpoint=endpoint,
            generation_token=generation_token,
            image=image,
            seed=seed,
            request_sem=request_sem,
            shutdown=shutdown,
            log_id=log_id,
        )

        result = GenerationResult()
        if ply_content:
            result.generation_time = gen_time or 0.0
            result.size = len(ply_content)
            result.ply = await _upload_to_r2(r2, miner.hotkey, current_round, prompt.name, "ply", ply_content, log_id)

        if png_content:
            result.png = await _upload_to_r2(r2, miner.hotkey, current_round, prompt.name, "png", png_content, log_id)

        tracker.record_result(
            success=not result.is_failed(), is_overtime=result.is_overtime(settings.generation_timeout_seconds)
        )
        logger.info(f"{log_id}: {tracker.progress_str()}")

        miner.generations[prompt.name] = result
        await miner.save(git_batcher, current_round)


async def _generate_and_render_with_retries(
    endpoint: str,
    generation_token: str | None,
    image: bytes,
    seed: int,
    request_sem: asyncio.Semaphore,
    shutdown: GracefulShutdown,
    log_id: str,
) -> tuple[bytes | None, bytes | None, float | None]:
    """Generate and render with retries. Returns (ply_content, png_content, gen_time)."""
    last_ply = last_png = None
    last_time = None

    for attempt in range(settings.prompt_retry_attempts):
        if shutdown.should_stop:
            break

        if attempt > 0:
            logger.info(f"{log_id}: retry {attempt + 1}/{settings.prompt_retry_attempts}")

        gen_result = await generate(
            request_sem=request_sem,
            endpoint=endpoint,
            image=image,
            seed=seed,
            log_id=log_id,
            shutdown=shutdown,
            auth_token=generation_token,
        )
        if gen_result.content is None:
            break  # We make `settings.generation_http_attempts` to generate. If all fail, no need to retry.

        last_ply = gen_result.content
        last_time = gen_result.generation_time

        png = await render(settings.render_service_url, gen_result.content, log_id)
        if png is None:
            logger.warning(f"{log_id}: render failed ({attempt + 1}/{settings.prompt_retry_attempts})")
            continue

        last_png = png

        if last_time and last_time > settings.generation_timeout_seconds:
            logger.warning(f"{log_id}: overtime {last_time:.1f}s ({attempt + 1}/{settings.prompt_retry_attempts})")
            continue

        return last_ply, last_png, last_time

    if last_ply:
        logger.warning(f"{log_id}: exhausted {settings.prompt_retry_attempts} attempts")

    return last_ply, last_png, last_time


async def _upload_to_r2(
    r2: R2Client,
    hotkey: str,
    current_round: int,
    prompt_name: str,
    ext: str,
    data: bytes,
    log_id: str,
) -> str | None:
    """Upload to R2 and return CDN URL."""
    key = settings.storage_key_template.format(round=current_round, hotkey=hotkey, filename=f"{prompt_name}.{ext}")
    try:
        await r2.upload(key=key, data=data)
        return f"{settings.cdn_url}/{key}"
    except Exception as e:
        logger.error(f"{log_id}: {ext.upper()} upload failed: {e}")
        return None
