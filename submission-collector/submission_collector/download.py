import asyncio
import io
import random
from pathlib import Path

import httpx
import numpy as np
from loguru import logger
from subnet_common.competition.generations import (
    GenerationResult,
    GenerationsMap,
    GenerationSource,
    get_generations,
    save_generations,
)
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.state import CompetitionState
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.embeddings import calculate_embeddings
from subnet_common.git_batcher import GitBatcher
from subnet_common.r2_client import R2Client
from subnet_common.render import GRAY_BG, GRAY_VIEWS, WHITE_BG, WHITE_VIEWS, render_grid, render_views
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from submission_collector.discord import NULL_DISCORD_NOTIFIER, DiscordNotifier
from submission_collector.settings import Settings


class DownloadPipeline:
    """Downloads miner .js modules, renders previews, uploads to R2."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        r2: R2Client,
        http_client: httpx.AsyncClient,
        settings: Settings,
        discord: DiscordNotifier = NULL_DISCORD_NOTIFIER,
    ) -> None:
        self.git_batcher = git_batcher
        self.r2 = r2
        self.http_client = http_client
        self.settings = settings
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._discord = discord

    async def run(self, state: CompetitionState, ref: str) -> dict[str, GenerationsMap]:
        """Download generated files from miner CDNs, render previews, and upload to R2.

        Processes submissions in random order to avoid bias.
        Supports resume - skips already completed generations.
        Saves progress after each generation for crash recovery.

        Returns hotkey → prompt → GenerationResult for all miners.
        """
        submissions = await require_submissions(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompt_urls = await require_prompts(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompts = [Path(url).stem for url in prompt_urls]
        # Prefetch prompt images once per round so embedding computation reuses the bytes
        # across all miners. Failures keep the run going — the affected stems just won't
        # have embeddings (and views=None for them, treated as preview-less by the judge).
        prompt_bytes = await self._fetch_prompt_images(prompt_urls)

        hotkeys = list(submissions.keys())
        random.shuffle(hotkeys)
        logger.info(f"Processing {len(hotkeys)} submissions × {len(prompts)} prompts")

        raw_results = await asyncio.gather(
            *[
                self._download_submission(
                    hotkey=hotkey,
                    submission=submissions[hotkey],
                    prompts=prompts,
                    prompt_bytes=prompt_bytes,
                    round_num=state.current_round,
                    ref=ref,
                )
                for hotkey in hotkeys
            ],
            return_exceptions=True,
        )
        generations_by_hotkey: dict[str, GenerationsMap] = {}
        for hotkey, result in zip(hotkeys, raw_results, strict=True):
            if isinstance(result, BaseException):
                logger.error(f"{hotkey[:10]}: download failed: {result}")
            else:
                generations_by_hotkey[hotkey] = result
        return generations_by_hotkey

    async def _fetch_prompt_images(self, prompt_urls: list[str]) -> dict[str, bytes]:
        """Download all prompt images for the round once. Stem -> bytes; missing on failure."""

        async def _one(url: str) -> tuple[str, bytes | None]:
            stem = Path(url).stem
            try:
                response = await self.http_client.get(url, timeout=httpx.Timeout(60, connect=10))
                response.raise_for_status()
                return stem, response.content
            except Exception as e:
                logger.warning(f"prompt image fetch failed for {stem} ({url}): {e}")
                return stem, None

        results = await asyncio.gather(*[_one(url) for url in prompt_urls])
        return {stem: data for stem, data in results if data is not None}

    async def _download_submission(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompts: list[str],
        prompt_bytes: dict[str, bytes],
        round_num: int,
        ref: str,
    ) -> GenerationsMap:
        """Download all outputs for one miner, saving progress after each prompt.

        Returns the final prompt → GenerationResult dict for this miner.
        """
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
        else:
            logger.info(f"Processing {len(prompts_to_process)}/{len(prompts)} prompts for {hotkey[:10]}")

            tasks = [
                self._fetch_render_upload(
                    hotkey=hotkey,
                    submission=submission,
                    prompt=prompt,
                    prompt_image=prompt_bytes.get(prompt),
                    round_num=round_num,
                    generations=generations,
                )
                for prompt in prompts_to_process
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for prompt, result in zip(prompts_to_process, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(f"{hotkey[:10]} / {prompt}: fetch failed: {result}")

        return {p: generations[p] for p in prompts if p in generations}

    async def _fetch_render_upload(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompt: str,
        prompt_image: bytes | None,
        round_num: int,
        generations: GenerationsMap,
    ) -> None:
        """Fetch .js, render 12 views + grid, compute embeddings, upload everything to R2.

        Atomic over the preview bundle: if any render, embedding, or upload fails,
        `views` stays None — the judge treats the stem as preview-less.

        On any per-stem failure, `failure_reason` is set to a short categorical string
        so operators can distinguish "miner CDN 404'd" from "render service died" from
        "miner truly didn't submit" by reading the persisted GenerationResult.

        generations: Mutable dict updated in-place with results. Used as a shared state
            across concurrent tasks - each task writes its prompt's result and persists
            the entire dict, enabling crash recovery.
        """
        log_id = f"{hotkey[:10]} / {prompt}"
        template = self.settings.storage_key_template

        def key_for(filename: str) -> str:
            return template.format(round=round_num, hotkey=hotkey, filename=filename)

        render_key = self.settings.render_api_key.get_secret_value() if self.settings.render_api_key else None

        js_url: str | None = None
        views_url: str | None = None
        size = 0
        failure_reason: str | None = None

        if self.settings.download_jitter_seconds > 0:
            jitter = random.uniform(0, self.settings.download_jitter_seconds)  # nosec B311 # noqa: S311
            await asyncio.sleep(jitter)

        async with self.semaphore:
            # Phase 1: fetch JS from miner CDN.
            try:
                js_data = await _fetch_js(
                    cdn_url=submission.cdn_url,
                    prompt=prompt,
                    log_id=log_id,
                    max_js_size_bytes=self.settings.max_js_size_bytes,
                )
                size = len(js_data)
            except Exception as e:
                # Unwrap tenacity RetryError so we see the underlying cause (status + URL).
                cause: BaseException = e
                if isinstance(e, RetryError) and e.last_attempt is not None:
                    inner = e.last_attempt.exception()
                    if inner is not None:
                        cause = inner
                if isinstance(cause, httpx.HTTPStatusError):
                    failure_reason = f"js_fetch_failed: HTTP {cause.response.status_code}"
                    logger.warning(f"{log_id}: {failure_reason} on {cause.request.url}")
                elif isinstance(cause, ValueError):
                    # Only ValueError raised by _fetch_js is the size cap.
                    failure_reason = "js_too_large"
                    logger.warning(f"{log_id}: {failure_reason}: {cause}")
                else:
                    failure_reason = f"js_fetch_failed: {type(cause).__name__}"
                    logger.warning(f"{log_id}: {failure_reason}: {cause}")
                generations[prompt] = GenerationResult(failure_reason=failure_reason)
                await save_generations(
                    git_batcher=self.git_batcher,
                    round_num=round_num,
                    hotkey=hotkey,
                    source=GenerationSource.SUBMITTED,
                    generations=generations,
                )
                return

            # Phase 2: upload JS. If this fails, we still record the miner delivered
            # (we just couldn't store it) — but `js` stays None so the judge skips it.
            try:
                js_url = await _upload_to_r2(
                    self.r2, key_for(f"{prompt}.js"), js_data, "application/javascript", log_id, self.settings.cdn_url
                )
            except Exception as e:
                failure_reason = "js_upload_failed"
                logger.error(f"{log_id}: {failure_reason}: {type(e).__name__}: {e}")

            # Phase 3: render views + grid in parallel. Any None here means a render
            # service / preview pipeline problem — distinct from miner failures.
            if failure_reason is None:
                white, gray, grid = await asyncio.gather(
                    render_views(
                        client=self.http_client,
                        endpoint=self.settings.render_service_url,
                        js_content=js_data,
                        views=WHITE_VIEWS,
                        bg_color=WHITE_BG,
                        log_id=log_id,
                        api_key=render_key,
                    ),
                    render_views(
                        client=self.http_client,
                        endpoint=self.settings.render_service_url,
                        js_content=js_data,
                        views=GRAY_VIEWS,
                        bg_color=GRAY_BG,
                        log_id=log_id,
                        api_key=render_key,
                    ),
                    render_grid(
                        client=self.http_client,
                        endpoint=self.settings.render_service_url,
                        js_content=js_data,
                        log_id=log_id,
                        api_key=render_key,
                    ),
                )

                if prompt_image is None:
                    failure_reason = "prompt_image_missing"
                    logger.warning(f"{log_id}: {failure_reason}; skipping embeddings/views bundle")
                elif white is None or gray is None:
                    failure_reason = "render_failed"
                    await self._discord.notify_render_failure(hotkey)
                elif grid is None:
                    failure_reason = "grid_render_failed"
                else:
                    # Phase 4: embeddings + bundle upload. Atomic — either views_url is
                    # set (every PNG and the npz uploaded) or failure_reason is set.
                    try:
                        hf_token = self.settings.hf_token.get_secret_value() if self.settings.hf_token else None
                        npz_bytes = await _build_embeddings_npz(
                            prompt_image,
                            white,
                            log_id,
                            hf_token=hf_token,
                        )
                    except Exception as e:
                        logger.error(f"{log_id}: embeddings_failed: {type(e).__name__}: {e}")
                        failure_reason = "embeddings_failed"
                        npz_bytes = None

                    if npz_bytes is not None:
                        bundle = (
                            [
                                (key_for(f"{prompt}/white/{view.name}.png"), white[view.name], "image/png")
                                for view in WHITE_VIEWS
                            ]
                            + [
                                (key_for(f"{prompt}/gray/{view.name}.png"), gray[view.name], "image/png")
                                for view in GRAY_VIEWS
                            ]
                            + [
                                (key_for(f"{prompt}/grid.png"), grid, "image/png"),
                                (key_for(f"{prompt}/embeddings.npz"), npz_bytes, "application/octet-stream"),
                            ]
                        )
                        try:
                            await asyncio.gather(
                                *[
                                    _upload_to_r2(self.r2, key, data, ctype, log_id, self.settings.cdn_url)
                                    for key, data, ctype in bundle
                                ]
                            )
                            views_url = f"{self.settings.cdn_url}/{key_for(prompt)}"
                        except Exception as e:
                            failure_reason = "views_upload_failed"
                            logger.error(f"{log_id}: {failure_reason}: {type(e).__name__}: {e}")

        generations[prompt] = GenerationResult(
            js=js_url,
            views=views_url,
            failure_reason=failure_reason,
            size=size,
        )

        await save_generations(
            git_batcher=self.git_batcher,
            round_num=round_num,
            hotkey=hotkey,
            source=GenerationSource.SUBMITTED,
            generations=generations,
        )


async def _build_embeddings_npz(
    prompt_bytes: bytes,
    white_views: dict[str, bytes],
    log_id: str,
    *,
    hf_token: str | None,
) -> bytes:
    """Compute DINOv3 embeddings for prompt + 8 white views; pack into a single .npz."""
    view_names = [v.name for v in WHITE_VIEWS]
    view_bytes = [white_views[name] for name in view_names]

    logger.debug(f"{log_id}: computing embeddings for prompt + {len(view_names)} views")
    start = asyncio.get_running_loop().time()
    embeds = await calculate_embeddings([prompt_bytes, *view_bytes], hf_token=hf_token)
    elapsed = asyncio.get_running_loop().time() - start
    if embeds.shape[0] != 1 + len(view_names):
        raise RuntimeError(f"expected {1 + len(view_names)} embeddings, got {embeds.shape[0]}")

    arrays: dict[str, np.ndarray] = {"prompt": embeds[0]}
    for i, name in enumerate(view_names, start=1):
        arrays[f"view_{name}"] = embeds[i]

    buf = io.BytesIO()
    np.savez(buf, **arrays)  # type: ignore[arg-type]  # numpy stubs miss the **kwargs form
    logger.debug(f"{log_id}: embeddings computed in {elapsed:.1f}s; " f"npz packed ({buf.tell() / 1024:.1f}KB)")
    return buf.getvalue()


async def _upload_to_r2(r2: R2Client, key: str, data: bytes, content_type: str, log_id: str, cdn_url: str) -> str:
    """Upload data to R2 and return the CDN URL."""
    start = asyncio.get_running_loop().time()
    await r2.upload(key=key, data=data, content_type=content_type)
    elapsed = asyncio.get_running_loop().time() - start
    logger.debug(f"{log_id}: uploaded {key} in {elapsed:.1f}s, {len(data) / 1024:.1f}KB")
    return f"{cdn_url}/{key}"


@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(3),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError)),
)
async def _fetch_js(cdn_url: str, prompt: str, log_id: str, max_js_size_bytes: int) -> bytes:
    """Fetch .js module from miner CDN with retries and size limit."""
    url = f"{cdn_url.rstrip('/')}/{prompt}.js"
    logger.debug(f"{log_id}: downloading {url}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_js_size_bytes:
                raise ValueError(f"JS size {content_length} exceeds limit {max_js_size_bytes}")

            chunks = []
            total_size = 0
            async for chunk in response.aiter_bytes():
                total_size += len(chunk)
                if total_size > max_js_size_bytes:
                    raise ValueError(f"JS size exceeds limit {max_js_size_bytes}")
                chunks.append(chunk)

            data = b"".join(chunks)

    logger.debug(f"{log_id}: downloaded {len(data) / 1024:.1f}KB")
    return data
