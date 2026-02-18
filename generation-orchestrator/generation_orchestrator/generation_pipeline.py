import asyncio
from collections.abc import Callable

import aiofiles
import httpx
from loguru import logger
from subnet_common.competition.generations import GenerationResult, GenerationSource, save_generations
from subnet_common.git_batcher import GitBatcher
from subnet_common.r2_client import R2Client
from subnet_common.render import render

from generation_orchestrator.generate import generate
from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.image_distance import measure_distance
from generation_orchestrator.pod_tracker import PodTracker
from generation_orchestrator.prompt_coordinator import PromptCoordinator, PromptEntry
from generation_orchestrator.settings import Settings


class GenerationPipeline:
    """Processes prompts through generate → render → upload → measure → save."""

    def __init__(
        self,
        settings: Settings,
        pod_endpoint: str,
        generation_token: str | None,
        coordinator: PromptCoordinator,
        git_batcher: GitBatcher,
        hotkey: str,
        current_round: int,
        seed: int,
        stop: GenerationStop,
    ):
        self._settings = settings
        self._pod_endpoint = pod_endpoint
        self._generation_token = generation_token
        self._coordinator = coordinator
        self._git_batcher = git_batcher
        self._hotkey = hotkey
        self._current_round = current_round
        self._seed = seed
        self._stop = stop

    async def run(self, pod_id: str, tracker: PodTracker) -> None:
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
                self._process_loop(pod_id, gpu_generation_sem, r2, tracker, service_client)
                for _ in range(self._settings.max_concurrent_prompts_per_pod)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_loop(
        self,
        pod_id: str,
        gpu_generation_sem: asyncio.Semaphore,
        r2: R2Client,
        tracker: PodTracker,
        service_client: httpx.AsyncClient,
    ) -> None:
        """Process prompts until all done, pod bad, or stopped."""

        short_id = pod_id[len("miner-00-") :]

        while not self._stop.should_stop:
            terminate, reason = tracker.should_terminate()
            if terminate:
                tracker.mark_pod_bad(short_id, reason)
                return

            if self._coordinator.mismatch_count >= 2 * self._settings.max_mismatched_prompts:
                self._coordinator.log_mismatch_limit_once(short_id)
                tracker.termination_reason = "mismatch limit"
                return

            allow_retry = tracker.should_accept_retries()
            entry, is_retry = self._coordinator.assign(short_id, allow_retry=allow_retry)

            if entry is None:
                if not self._coordinator.has_work(short_id, allow_retry=allow_retry):
                    tracker.termination_reason = "no work available"
                    logger.debug(f"{short_id}: no work available")
                    return
                if not await self._coordinator.wait_for_change(self._stop):
                    return
                continue

            await self._process_entry(
                entry=entry,
                short_id=short_id,
                gpu_generation_sem=gpu_generation_sem,
                r2=r2,
                tracker=tracker,
                service_client=service_client,
                is_retry=is_retry,
            )

    async def _process_entry(
        self,
        entry: PromptEntry,
        short_id: str,
        gpu_generation_sem: asyncio.Semaphore,
        r2: R2Client,
        tracker: PodTracker,
        service_client: httpx.AsyncClient,
        is_retry: bool,
    ) -> None:
        stem = entry.prompt.stem
        log_id = f"{short_id} / {stem}"

        async with aiofiles.open(entry.prompt.path, "rb") as f:
            image = await f.read()

        result = await self._generate_render_upload(
            image=image,
            gpu_generation_sem=gpu_generation_sem,
            r2=r2,
            service_client=service_client,
            stem=stem,
            log_id=log_id,
            is_canceled=lambda: entry.done or self._stop.should_stop,
        )

        if result is None:
            return  # Canceled. Lock expires on its own.

        if entry.submitted_png is not None and result.png is not None:
            result.distance = await self._measure_distance(service_client, result.png, entry.submitted_png, log_id)

        original_time: float | None = None
        if is_retry and entry.best_result is not None and not entry.best_result.is_failed():
            original_time = entry.best_result.generation_time

        tracker.record(
            success=not result.is_failed(),
            generation_time=result.generation_time,
            is_retry=is_retry,
            original_time=original_time,
        )

        updated = self._coordinator.record_result(short_id, stem, result)
        if updated:
            await save_generations(
                git_batcher=self._git_batcher,
                hotkey=self._hotkey,
                round_num=self._current_round,
                generations=self._coordinator.generations,
                source=GenerationSource.GENERATED,
            )

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
            generation_timeout=self._settings.generation_timeout_seconds,
            download_timeout=self._settings.download_timeout_seconds,
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
        result.glb = await self._upload(r2, stem, "glb", gen_result.content, log_id)

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
