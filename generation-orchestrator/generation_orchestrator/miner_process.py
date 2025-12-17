import asyncio

import aiofiles
from loguru import logger
from pydantic import BaseModel
from subnet_common.competition.generations import GenerationResult, get_miner_generations, save_miner_generations
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown

from generation_orchestrator.generate import generate
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.r2_client import R2Client
from generation_orchestrator.render import render
from generation_orchestrator.settings import settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore
from generation_orchestrator.targon import ensure_running_container
from generation_orchestrator.targon_client import ContainerDeployConfig, TargonClient


class Miner(BaseModel):
    hotkey: str
    docker_image: str
    generations: dict[str, GenerationResult]


class ProcessingStats(BaseModel):
    """Stats from processing a miner's prompts."""

    total: int
    successful: int  # ply + png + within the time limit
    failed: int  # no png
    overtime: int  # has output but exceeded the time limit


def _calculate_stats(generations: dict[str, GenerationResult], prompts: list[Prompt]) -> ProcessingStats:
    """Calculate processing stats for completed prompts."""
    successful = 0
    failed = 0
    overtime = 0

    timeout = settings.generation_timeout_seconds

    for p in prompts:
        gen = generations.get(p.name)
        if gen is None or gen.is_failed():
            failed += 1
        elif gen.is_overtime(timeout):
            overtime += 1
        else:
            successful += 1

    return ProcessingStats(total=len(prompts), successful=successful, failed=failed, overtime=overtime)


def _get_remaining_prompts(
    generations: dict[str, GenerationResult],
    prompts: list[Prompt],
) -> list[Prompt]:
    """Get prompts that still need processing (not done or need retry)."""
    timeout = settings.generation_timeout_seconds
    remaining = []
    for p in prompts:
        gen = generations.get(p.name)
        if gen is None or gen.needs_retry(timeout):
            remaining.append(p)
    return remaining


def _should_retry_with_new_pod(stats: ProcessingStats) -> tuple[bool, str]:
    """Decide if failure/overtime counts warrant a new pod.

    Returns (should_retry, reason).
    """
    if stats.total == 0:
        return True, "no prompts processed"

    fail_rate = stats.failed / stats.total

    if fail_rate > settings.max_fail_rate:
        return True, f"fail rate {fail_rate:.1%} > {settings.max_fail_rate:.1%}"

    overtime_tolerance = stats.total * settings.overtime_tolerance_ratio

    if stats.overtime > overtime_tolerance:
        return True, f"overtime count {stats.overtime} > {overtime_tolerance}"

    return False, ""


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
    log_id = hotkey[:10]

    for attempt in range(settings.miner_process_attempts):
        try:
            async with semaphore:
                success, stats = await process_miner(
                    git_batcher=git_batcher,
                    hotkey=hotkey,
                    docker_image=docker_image,
                    current_round=current_round,
                    prompts=prompts,
                    seed=seed,
                    shutdown=shutdown,
                )

                if not success:
                    logger.info(f"{log_id}: attempt {attempt + 1} failed (pod issue)")
                    continue

                logger.info(
                    f"{log_id}: attempt {attempt + 1} - "
                    f"{stats.successful} ok, {stats.failed} failed, {stats.overtime} overtime"
                )

                if stats.failed == 0 and stats.overtime == 0:
                    return

                should_retry, reason = _should_retry_with_new_pod(stats)
                if not should_retry:
                    logger.info(f"{log_id}: rates acceptable, done")
                    return

                # Will retry with a new pod
                logger.info(f"{log_id}: {reason}, will retry {stats.failed + stats.overtime} prompts with new pod")

        except Exception as e:
            logger.exception(f"{log_id}: attempt {attempt + 1} failed: {e}")

    logger.error(f"{log_id}: all {settings.miner_process_attempts} attempts failed")


async def process_miner(
    git_batcher: GitBatcher,
    hotkey: str,
    docker_image: str,
    current_round: int,
    prompts: list[Prompt],
    seed: int,
    shutdown: GracefulShutdown,
) -> tuple[bool, ProcessingStats]:
    """Process a miner's prompts.

    Returns:
        (success, stats) where success indicates pod worked, stats have prompt outcomes.
    """
    log_id = hotkey[:10]

    generations = await get_miner_generations(
        git=git_batcher.git, hotkey=hotkey, round_num=current_round, ref=settings.github_branch
    )  # Safe to load the progress here, as there is no concurrent activity that updates it.

    miner = Miner(
        hotkey=hotkey,
        docker_image=docker_image,
        generations=generations,
    )

    remaining = _get_remaining_prompts(generations, prompts)
    if not remaining:
        logger.info(f"{log_id}: already completed all prompts")
        return True, _calculate_stats(miner.generations, prompts)

    logger.info(f"{log_id}: {len(miner.generations)} done, {len(remaining)} remaining")

    async with TargonClient(api_key=settings.targon_api_key.get_secret_value()) as targon:
        config = ContainerDeployConfig(
            image=docker_image,
            resource_name=settings.targon_resource,
            port=settings.generation_port,
            container_concurrency=settings.max_concurrent_prompts_per_miner + 1,
        )
        container_name = f"miner-{current_round}-{miner.hotkey[:10].lower()}"
        container = await ensure_running_container(
            targon,
            name=container_name,
            config=config,
            shutdown=GracefulShutdown(),
            reuse_existing=settings.debug_keep_pods_alive,
            deploy_timeout=settings.targon_startup_timeout_seconds,
            warmup_timeout=settings.targon_warmup_timeout_seconds,
            check_interval=settings.check_pod_interval_seconds,
        )
        if container is None:
            await targon.delete_containers_by_name(container_name)
            return False, _calculate_stats(miner.generations, prompts)

        if container.url is None:
            await targon.delete_container(container.uid)
            return False, _calculate_stats(miner.generations, prompts)

        try:
            await process_all_prompts(
                git_batcher=git_batcher,
                endpoint=container.url,
                current_round=current_round,
                prompts=remaining,
                seed=seed,
                shutdown=shutdown,
                miner=miner,
            )
        finally:
            if not settings.debug_keep_pods_alive:
                await targon.delete_container(container.uid)

    return True, _calculate_stats(miner.generations, prompts)


async def process_all_prompts(
    git_batcher: GitBatcher,
    miner: Miner,
    current_round: int,
    endpoint: str,
    prompts: list[Prompt],
    seed: int,
    shutdown: GracefulShutdown,
) -> None:
    request_sem = asyncio.Semaphore(1)  # Using semaphores to limit request to one at a time.
    process_sem = asyncio.Semaphore(settings.max_concurrent_prompts_per_miner)  # Limiting request to control traffic

    async with R2Client(
        access_key_id=settings.r2_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        r2_endpoint=settings.r2_endpoint.get_secret_value(),
    ) as r2:
        tasks = [
            process_prompt(
                git_batcher=git_batcher,
                request_sem=request_sem,
                process_sem=process_sem,
                endpoint=endpoint,
                prompt=prompt,
                current_round=current_round,
                seed=seed,
                r2=r2,
                miner=miner,
                shutdown=shutdown,
            )
            for prompt in prompts
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for prompt, result in zip(prompts, results, strict=True):
            if isinstance(result, Exception):
                logger.error(f"{miner.hotkey[:10]}/{prompt.name}: {result}")


async def process_prompt(
    git_batcher: GitBatcher,
    request_sem: asyncio.Semaphore,
    process_sem: asyncio.Semaphore,
    endpoint: str,
    prompt: Prompt,
    seed: int,
    current_round: int,
    r2: R2Client,
    miner: Miner,
    shutdown: GracefulShutdown,
) -> None:
    log_id = f"{miner.hotkey[:10]}/{prompt.name}"

    async with process_sem:
        if shutdown.should_stop:
            return

        async with aiofiles.open(prompt.path, "rb") as f:
            image = await f.read()

        ply_key = make_storage_key(miner.hotkey, current_round, prompt.name, "ply")
        png_key = make_storage_key(miner.hotkey, current_round, prompt.name, "png")

        result = await _generate_and_render_with_retries(
            request_sem=request_sem,
            endpoint=endpoint,
            image=image,
            seed=seed,
            shutdown=shutdown,
            log_id=log_id,
        )

        single_gen = GenerationResult()

        if result.ply_content is not None:
            single_gen.generation_time = result.generation_time or 0.0
            single_gen.size = len(result.ply_content)

            # Upload PLY (always, for analysis even if render failed)
            try:
                await r2.upload(key=ply_key, data=result.ply_content)
                single_gen.ply = make_cdn_url(ply_key)
            except Exception as e:
                logger.error(f"{log_id}: PLY upload failed: {e}")

        if result.png_content is not None:
            try:
                await r2.upload(key=png_key, data=result.png_content)
                single_gen.png = make_cdn_url(png_key)
            except Exception as e:
                logger.error(f"{log_id}: PNG upload failed: {e}")

    miner.generations[prompt.name] = single_gen
    await save_miner_generations(
        git_batcher=git_batcher, hotkey=miner.hotkey, round_num=current_round, generations=miner.generations
    )


class _GenerateAndRenderResult(BaseModel):
    """Result of generation and render cycle."""

    ply_content: bytes | None = None
    png_content: bytes | None = None
    generation_time: float | None = None


async def _generate_and_render_with_retries(
    request_sem: asyncio.Semaphore,
    endpoint: str,
    image: bytes,
    seed: int,
    shutdown: GracefulShutdown,
    log_id: str,
) -> _GenerateAndRenderResult:
    """Generate and render with prompt-level retries.

    Retries on: render failure, generation overtime.
    After all retries exhausted, returns the last result (even if overtime).
    """
    max_attempts = settings.prompt_retry_attempts
    last_ply: bytes | None = None
    last_png: bytes | None = None
    last_time: float | None = None

    for attempt in range(max_attempts):
        if shutdown.should_stop:
            break

        if attempt > 0:
            logger.info(f"{log_id}: prompt retry {attempt + 1}/{max_attempts}")

        gen_result = await generate(
            request_sem=request_sem,
            endpoint=endpoint,
            image=image,
            seed=seed,
            shutdown=shutdown,
            log_id=log_id,
        )

        if gen_result.content is None:
            break  # Generation failed, no point retrying

        last_ply = gen_result.content
        last_time = gen_result.generation_time

        png_content = await render(
            endpoint=settings.render_service_url,
            ply_content=gen_result.content,
            log_id=log_id,
        )

        if png_content is None:
            logger.warning(f"{log_id}: render failed (attempt {attempt + 1}/{max_attempts})")
            continue

        last_png = png_content

        if last_time and last_time > settings.generation_timeout_seconds:
            logger.warning(f"{log_id}: overtime ({last_time:.1f}s) (attempt {attempt + 1}/{max_attempts})")
            continue

        # Full success: PLY + PNG + on time
        return _GenerateAndRenderResult(ply_content=last_ply, png_content=last_png, generation_time=last_time)

    if last_ply is not None:
        logger.warning(f"{log_id}: all {max_attempts} attempts exhausted")

    return _GenerateAndRenderResult(ply_content=last_ply, png_content=last_png, generation_time=last_time)


def make_storage_key(hotkey: str, current_round: int, prompt_name: str, ext: str) -> str:
    return settings.storage_key_template.format(
        round=current_round,
        hotkey=hotkey,
        filename=f"{prompt_name}.{ext}",
    )


def make_cdn_url(key: str) -> str:
    return f"{settings.cdn_url}/{key}"
