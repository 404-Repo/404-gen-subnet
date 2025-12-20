import asyncio

import aiofiles
from loguru import logger
from pydantic import BaseModel
from subnet_common.competition.generations import GenerationResult, get_miner_generations, save_miner_generations
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown
from targon.client.serverless import ServerlessResourceListItem

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
    for attempt in range(settings.miner_process_attempts):
        try:
            async with semaphore:
                if await process_miner(
                    git_batcher=git_batcher,
                    hotkey=hotkey,
                    docker_image=docker_image,
                    current_round=current_round,
                    prompts=prompts,
                    seed=seed,
                    shutdown=shutdown,
                ):
                    return
            logger.info(f"Attempt {attempt + 1} wasn't successful for {hotkey[:10]}.")
        except Exception as e:
            logger.exception(f"Attempt {attempt + 1} failed for {hotkey[:10]}: {e}")

    logger.error(f"All {settings.miner_process_attempts} attempts failed for {hotkey[:10]}")


async def process_miner(
    git_batcher: GitBatcher,
    hotkey: str,
    docker_image: str,
    current_round: int,
    prompts: list[Prompt],
    seed: int,
    shutdown: GracefulShutdown,
) -> bool:
    generations = await get_miner_generations(
        git=git_batcher.git, hotkey=hotkey, round_num=current_round, ref=settings.github_branch
    )  # Safe to load the progress here, as there is no concurrent activity that updates it.

    miner = Miner(
        hotkey=hotkey,
        docker_image=docker_image,
        generations=generations,
    )
    remaining = [p for p in prompts if p.name not in miner.generations]
    if not remaining:
        logger.info(f"Miner {miner.hotkey[:10]} already completed all prompts")
        return True

    logger.info(f"Miner {miner.hotkey[:10]}: {len(miner.generations)} done, {len(remaining)} remaining")

    async with TargonClient(api_key=settings.targon_api_key.get_secret_value()) as targon:
        config = ContainerDeployConfig(
            image=docker_image,
            resource_name=settings.targon_resource,
            port=settings.generation_port,
            container_concurrency=settings.max_concurrent_prompts_per_miner + 1,
        )
        container = await create_container(
            targon=targon,
            current_round=current_round,
            miner=miner,
            config=config,
        )
        if container is None:
            return False

        try:
            await process_all_prompts(
                git_batcher=git_batcher,
                endpoint=container.url,
                current_round=current_round,
                prompts=remaining,
                seed=seed,
                shutdown=shutdown,
                miner=miner,
                targon=targon,
                config=config,
            )
        finally:
            if not settings.debug_keep_pods_alive:
                await targon.delete_container(container.uid)

    return True


def get_container_name(current_round: int, miner: Miner) -> str:
    return f"miner-{current_round}-{miner.hotkey[:10].lower()}"


async def create_container(
    targon: TargonClient,
    current_round: int,
    miner: Miner,
    config: ContainerDeployConfig,
) -> ServerlessResourceListItem | None:
    container_name = get_container_name(current_round, miner)
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
        return None

    if container.url is None:
        await targon.delete_container(container.uid)
        return None
    return container


async def process_all_prompts(
    git_batcher: GitBatcher,
    miner: Miner,
    current_round: int,
    endpoint: str,
    prompts: list[Prompt],
    seed: int,
    shutdown: GracefulShutdown,
    targon: TargonClient,
    config: ContainerDeployConfig,
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
                targon=targon,
                config=config,
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
    targon: TargonClient,
    config: ContainerDeployConfig,
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

    failure_count = len(
        [
            g
            for g in miner.generations.values()
            if g.ply is None or g.generation_time >= settings.generation_timeout_seconds
        ]
    )
    if failure_count >= settings.generation_failure_threshold:
        logger.error(f"{log_id}: Generation failure threshold reached, restarting pod")
        container_name = get_container_name(current_round, miner)
        await targon.delete_containers_by_name(container_name)
        container = await create_container(
            targon=targon,
            current_round=current_round,
            miner=miner,
            config=config,
        )
        if container is not None:
            logger.error(f"{log_id}: Error restarting pod")


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

    If render fails (likely bad PLY), regenerate and try again.
    After all retries are exhausted, returns last PLY content (for analysis) even without PNG.
    """
    max_attempts = settings.prompt_retry_attempts
    last_ply_content: bytes | None = None
    last_generation_time: float | None = None

    for attempt in range(max_attempts):
        if shutdown.should_stop:
            break

        if attempt > 0:
            logger.info(f"{log_id}: Prompt retry {attempt + 1}/{max_attempts}")

        gen_result = await generate(
            request_sem=request_sem,
            endpoint=endpoint,
            image=image,
            seed=seed,
            shutdown=shutdown,
            log_id=log_id,
        )

        if gen_result.content is None:
            # Generation failed after HTTP retries — no point retrying at the prompt level
            break

        last_ply_content = gen_result.content
        last_generation_time = gen_result.generation_time

        png_content = await render(
            endpoint=settings.render_service_url,
            ply_content=gen_result.content,
            log_id=log_id,
        )

        if png_content is not None:
            return _GenerateAndRenderResult(
                ply_content=gen_result.content,
                png_content=png_content,
                generation_time=gen_result.generation_time,
            )

        logger.warning(f"{log_id}: render failed, will regenerate (attempt {attempt + 1}/{max_attempts})")

    # All retries exhausted — return last PLY for analysis (if any)
    if last_ply_content is not None:
        logger.warning(f"{log_id}: all {max_attempts} prompt attempts failed, saving last PLY for analysis")

    return _GenerateAndRenderResult(
        ply_content=last_ply_content,
        png_content=None,
        generation_time=last_generation_time,
    )


def make_storage_key(hotkey: str, current_round: int, prompt_name: str, ext: str) -> str:
    return settings.storage_key_template.format(
        round=current_round,
        hotkey=hotkey,
        filename=f"{prompt_name}.{ext}",
    )


def make_cdn_url(key: str) -> str:
    return f"{settings.cdn_url}/{key}"
