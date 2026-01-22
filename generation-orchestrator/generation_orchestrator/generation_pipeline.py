import asyncio
from collections.abc import Callable

import aiofiles
import httpx
from loguru import logger
from subnet_common.competition.generations import GenerationResult, GenerationSource, save_generations
from subnet_common.git_batcher import GitBatcher

from generation_orchestrator.failure_tracker import FailureTracker
from generation_orchestrator.generate import generate
from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.image_distance import measure_distance
from generation_orchestrator.prompt_queue import PromptQueue, PromptTask
from generation_orchestrator.r2_client import R2Client
from generation_orchestrator.render import render
from generation_orchestrator.settings import Settings


class GenerationPipeline:
    """Processes prompts through generate → render → upload → measure → save."""

    def __init__(
        self,
        settings: Settings,
        pod_endpoint: str,
        generation_token: str | None,
        queue: PromptQueue,  # Shared across all pods for this miner
        generations: dict[str, GenerationResult],  # Mutable shared dict, updated in place
        git_batcher: GitBatcher,
        hotkey: str,
        current_round: int,
        seed: int,
        stop: GenerationStop,
    ):
        self._settings = settings
        self._pod_endpoint = pod_endpoint
        self._generation_token = generation_token
        self._queue = queue
        self._generations = generations
        self._git_batcher = git_batcher
        self._hotkey = hotkey
        self._current_round = current_round
        self._seed = seed
        self._stop = stop

    async def run(self, worker_id: str, tracker: FailureTracker) -> None:
        """Start concurrent processing loops for this worker."""

        # GPU generation slot: released on first response bytes to keep GPU saturated.
        gpu_generation_sem = asyncio.Semaphore(1)
        service_timeout = httpx.Timeout(connect=60.0, read=120.0, write=120.0, pool=60.0)

        async with (
            R2Client(
                access_key_id=self._settings.r2_access_key_id.get_secret_value(),
                secret_access_key=self._settings.r2_secret_access_key.get_secret_value(),
                r2_endpoint=self._settings.r2_endpoint.get_secret_value(),
            ) as r2,
            httpx.AsyncClient(timeout=service_timeout) as service_client,
        ):
            tasks = [
                self._process_loop(worker_id, gpu_generation_sem, r2, tracker, service_client)
                for _ in range(self._settings.max_concurrent_prompts_per_pod)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _make_is_canceled(task: PromptTask) -> Callable[[], bool]:
        return lambda: task.completed

    async def _process_loop(
        self,
        worker_id: str,
        gpu_generation_sem: asyncio.Semaphore,
        r2: R2Client,
        tracker: FailureTracker,
        service_client: httpx.AsyncClient,
    ) -> None:
        """Process prompts until queue empty, pod bad, or stopped."""

        short_id = worker_id[len("miner-00-") :]

        while not self._stop.should_stop:
            is_pod_bad, reason = tracker.is_pod_bad()
            if is_pod_bad:
                tracker.log_bad_pod_once(worker_id, reason)
                break

            task = self._queue.get(worker_id=worker_id)
            if task is None:
                logger.debug(f"{short_id}: task queue exhausted")
                break

            async with aiofiles.open(task.prompt.path, "rb") as f:
                image = await f.read()

            stem = task.prompt.stem
            prompt_log_id = f"{short_id} / {stem}"

            result = await self._generate_render_upload(
                image=image,
                gpu_generation_sem=gpu_generation_sem,
                r2=r2,
                service_client=service_client,
                stem=stem,
                log_id=prompt_log_id,
                is_canceled=self._make_is_canceled(task),
            )

            if result is None:
                continue

            if task.submitted_png is not None and result.png is not None:
                result.distance = await self._measure_distance(
                    service_client, result.png, task.submitted_png, prompt_log_id
                )

            tracker.record(
                success=not result.is_failed(),
                generation_time=result.generation_time,
                distance=result.distance,
            )

            if not result.needs_retry(tracker.effective_timeout()):
                self._queue.complete(task)

            await self._update_generations(stem, result)

    async def _generate_render_upload(
        self,
        image: bytes,
        gpu_generation_sem: asyncio.Semaphore,
        r2: R2Client,
        service_client: httpx.AsyncClient,
        stem: str,
        log_id: str,
        is_canceled: Callable[[], bool] | None = None,
    ) -> GenerationResult | None:
        """Generate a 3D, render preview, upload both to R2.
        Returns None if a generation was canceled before starting.
        """
        gen_result = await generate(
            gpu_generation_sem=gpu_generation_sem,
            endpoint=self._pod_endpoint,
            image=image,
            seed=self._seed,
            log_id=log_id,
            auth_token=self._generation_token,
            is_canceled=is_canceled,
        )

        if gen_result is None:
            return None

        result = GenerationResult()
        if gen_result.content is None:
            return result

        result.generation_time = gen_result.generation_time or 0.0
        result.size = len(gen_result.content)
        result.ply = await self._upload(r2, stem, "ply", gen_result.content, log_id)

        png = await render(service_client, self._settings.render_service_url, gen_result.content, log_id)
        if png is None:
            logger.warning(f"{log_id}: render failed")
            return result

        result.png = await self._upload(r2, stem, "png", png, log_id)
        return result

    async def _upload(self, r2: R2Client, stem: str, ext: str, data: bytes, log_id: str) -> str | None:
        """Upload data to R2 and return CDN URL."""
        key = self._settings.storage_key_template.format(
            round=self._current_round, hotkey=self._hotkey, filename=f"{stem}.{ext}"
        )
        try:
            await r2.upload(key=key, data=data)
            return f"{self._settings.cdn_url}/{key}"
        except Exception as e:
            logger.error(f"{log_id}: {ext.upper()} upload failed: {e}")
            return None

    async def _measure_distance(
        self,
        service_client: httpx.AsyncClient,
        png_url: str,
        submitted_png: str,
        log_id: str,
    ) -> float:
        """Measure the distance between generated and submitted preview images."""
        measured = await measure_distance(
            client=service_client,
            endpoint=self._settings.image_distance_service_url,
            image_url_1=png_url,
            image_url_2=submitted_png,
            log_id=log_id,
        )
        if measured is None:
            logger.warning(f"{log_id}: failed to measure distance, accepting")
            return 0.0
        return measured

    async def _update_generations(self, stem: str, result: GenerationResult) -> None:
        """Update generations dict and save if a result is better than existing."""
        existing = self._generations.get(stem)
        existing_failed = existing is not None and (
            existing.is_failed() or existing.distance > self._settings.acceptable_distance
        )
        updated_failed = result.is_failed() or result.distance > self._settings.acceptable_distance

        should_update = (
            existing is None
            or existing_failed
            or (not updated_failed and existing.generation_time > result.generation_time)
        )

        if should_update:
            self._generations[stem] = result
            await save_generations(
                git_batcher=self._git_batcher,
                hotkey=self._hotkey,
                round_num=self._current_round,
                generations=self._generations,
                source=GenerationSource.GENERATED,
            )
