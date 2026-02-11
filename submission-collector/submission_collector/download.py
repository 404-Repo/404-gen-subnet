import asyncio
import random
from pathlib import Path

import httpx
from loguru import logger
from subnet_common.competition.generations import GenerationResult, GenerationSource, get_generations, save_generations
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.state import CompetitionState
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.git_batcher import GitBatcher
from subnet_common.r2_client import R2Client
from subnet_common.render import render
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from submission_collector.settings import Settings


class DownloadPipeline:
    """Downloads miner GLBs, renders previews, uploads to R2."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        r2: R2Client,
        http_client: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self.git_batcher = git_batcher
        self.r2 = r2
        self.http_client = http_client
        self.settings = settings
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)

    async def run(self, state: CompetitionState, ref: str) -> None:
        """Download generated files from miner CDNs, render previews, and upload to R2.

        Processes submissions in random order to avoid bias.
        Supports resume - skips already completed generations.
        Saves progress after each generation for crash recovery.
        """
        submissions = await require_submissions(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompt_urls = await require_prompts(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompts = [Path(url).stem for url in prompt_urls]

        hotkeys = list(submissions.keys())
        random.shuffle(hotkeys)
        logger.info(f"Processing {len(hotkeys)} submissions × {len(prompts)} prompts")

        await asyncio.gather(
            *[
                self._download_submission(
                    hotkey=hotkey,
                    submission=submissions[hotkey],
                    prompts=prompts,
                    round_num=state.current_round,
                    ref=ref,
                )
                for hotkey in hotkeys
            ]
        )

    async def _download_submission(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompts: list[str],
        round_num: int,
        ref: str,
    ) -> None:
        """Download all outputs for one miner, saving progress after each prompt."""
        generations = await get_generations(
            git=self.git_batcher.git,
            round_num=round_num,
            hotkey=hotkey,
            source=GenerationSource.SUBMITTED,
            ref=ref,
        )

        prompts_to_process = [p for p in prompts if p not in generations]

        if not prompts_to_process:
            logger.info(f"Skipping {hotkey[:10]}: all prompts complete")
            return

        logger.info(f"Processing {len(prompts_to_process)}/{len(prompts)} prompts for {hotkey[:10]}")

        tasks = [
            self._fetch_render_upload(
                hotkey=hotkey,
                submission=submission,
                prompt=prompt,
                round_num=round_num,
                generations=generations,
            )
            for prompt in prompts_to_process
        ]
        await asyncio.gather(*tasks)

    async def _fetch_render_upload(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompt: str,
        round_num: int,
        generations: dict[str, GenerationResult],
    ) -> None:
        """Fetch GLB from miner CDN, render PNG, upload both to R2, save progress.

        generations: Mutable dict updated in-place with results. Used as a shared state
            across concurrent tasks - each task writes its prompt's result and persists
            the entire dict, enabling crash recovery.
        """
        log_id = f"{hotkey[:10]} / {prompt}"

        def make_key(ext: str) -> str:
            return self.settings.storage_key_template.format(round=round_num, hotkey=hotkey, filename=f"{prompt}.{ext}")

        try:
            if self.settings.download_jitter_seconds > 0:
                jitter = random.uniform(0, self.settings.download_jitter_seconds)  # nosec B311 # noqa: S311
                await asyncio.sleep(jitter)

            async with self.semaphore:
                glb_data = await _fetch_glb(
                    cdn_url=submission.cdn_url,
                    prompt=prompt,
                    log_id=log_id,
                    max_glb_size_bytes=self.settings.max_glb_size_bytes,
                )
                glb_url = await _upload_to_r2(
                    self.r2, make_key("glb"), glb_data, "application/octet-stream", log_id, self.settings.cdn_url
                )

                png_data = await render(
                    client=self.http_client,
                    endpoint=self.settings.render_service_url,
                    glb_content=glb_data,
                    log_id=log_id,
                )

                png_url = None
                if png_data is not None:
                    png_url = await _upload_to_r2(
                        self.r2, make_key("png"), png_data, "image/png", log_id, self.settings.cdn_url
                    )

            generations[prompt] = GenerationResult(
                glb=glb_url,
                png=png_url,
                size=len(glb_data),
            )
        except Exception as e:
            logger.warning(f"{log_id}: failed with {e}")
            generations[prompt] = GenerationResult()

        await save_generations(
            git_batcher=self.git_batcher,
            round_num=round_num,
            hotkey=hotkey,
            source=GenerationSource.SUBMITTED,
            generations=generations,
        )


async def _upload_to_r2(r2: R2Client, key: str, data: bytes, content_type: str, log_id: str, cdn_url: str) -> str:
    """Upload data to R2 and return the CDN URL."""
    start = asyncio.get_running_loop().time()
    await r2.upload(key=key, data=data, content_type=content_type)
    elapsed = asyncio.get_running_loop().time() - start
    logger.debug(f"{log_id}: uploaded in {elapsed:.1f}s, {len(data) / 1024:.1f}KB")
    return f"{cdn_url}/{key}"


@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(3),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError)),
)
async def _fetch_glb(cdn_url: str, prompt: str, log_id: str, max_glb_size_bytes: int) -> bytes:
    """Fetch GLB file from miner CDN with retries and size limit."""
    url = f"{cdn_url.rstrip('/')}/{prompt}.glb"
    logger.debug(f"{log_id}: downloading {url}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_glb_size_bytes:
                raise ValueError(f"GLB size {content_length} exceeds limit {max_glb_size_bytes}")

            chunks = []
            total_size = 0
            async for chunk in response.aiter_bytes():
                total_size += len(chunk)
                if total_size > max_glb_size_bytes:
                    raise ValueError(f"GLB size exceeds limit {max_glb_size_bytes}")
                chunks.append(chunk)

            data = b"".join(chunks)

    logger.debug(f"{log_id}: downloaded {len(data) / 1024 / 1024:.1f}MB")
    return data
