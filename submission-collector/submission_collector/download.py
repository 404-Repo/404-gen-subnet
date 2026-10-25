import asyncio
import hashlib
import random
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx
from loguru import logger
from subnet_common.competition.generations import (
    GenerationResult,
    GenerationsMap,
    get_generations,
    save_generations,
)
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import CompetitionState
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.embeddings import build_embeddings_npz
from subnet_common.git_batcher import GitBatcher
from subnet_common.r2_client import R2Client
from subnet_common.render import GRAY_BG, GRAY_VIEWS, WHITE_BG, WHITE_VIEWS, render_grid, render_views

from submission_collector.artifacts import (
    ArtifactReadError,
    SelectedOutput,
    WitnessArtifacts,
)
from submission_collector.settings import Settings


_T = TypeVar("_T")


async def _join(group: Awaitable[_T], tasks: Sequence[asyncio.Future[Any]]) -> _T:
    """Join cancelled child work before its HTTP and storage clients can close."""
    try:
        return await group
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class DownloadPipeline:
    """Collect selected witness outputs and prepare judging bundles."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        r2: R2Client,
        http_client: httpx.AsyncClient,
        settings: Settings,
        artifacts: WitnessArtifacts,
    ) -> None:
        self.git_batcher = git_batcher
        self.r2 = r2
        self.http_client = http_client
        self.settings = settings
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.artifacts = artifacts
        self._seed: int | None = None

    async def run(self, state: CompetitionState, ref: str) -> dict[str, GenerationsMap]:
        """Collect generated sources, prepare previews, and upload to R2.

        Processes submissions in random order to avoid bias.
        Supports resume - skips already completed generations.
        Saves progress after each generation for crash recovery.

        Returns hotkey → prompt → GenerationResult for all miners.
        """
        submissions = await require_submissions(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompt_urls = await require_prompts(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompts = [Path(url).stem for url in prompt_urls]
        self._seed = await require_seed_from_git(self.git_batcher.git, state.current_round, ref)
        # Freeze each prompt image once for artifact bindings and embeddings across miners.
        prompt_bytes = await self._fetch_prompt_images(prompt_urls)
        if any(prompt not in prompt_bytes for prompt in prompts):
            raise ArtifactReadError("A frozen prompt image is unavailable")
        expected_hashes = set(self.artifacts.settings.prompt_hashes)
        actual_hashes = {hashlib.sha256(image).hexdigest() for image in prompt_bytes.values()}
        if (
            actual_hashes != expected_hashes
            or len(actual_hashes) != len(prompts)
            or any(submission.round != self.artifacts.settings.round for submission in submissions.values())
        ):
            raise ArtifactReadError("Collector configuration differs from the frozen round or prompts")

        hotkeys = list(submissions.keys())
        random.shuffle(hotkeys)
        logger.info(f"Processing {len(hotkeys)} submissions × {len(prompts)} prompts")

        # No return_exceptions: any escape from `_download_submission` is an
        # infrastructure error (typically a git read failing) or an unhandled bug —
        # both should restart the iteration, not silently mark the miner as zero.
        # Declared failures, missing submissions and validation rejections persist
        # as terminal outcomes; infrastructure failures remain retryable.
        tasks = [
            asyncio.create_task(
                self._download_submission(
                    hotkey=hotkey,
                    submission=submissions[hotkey],
                    prompts=prompts,
                    prompt_bytes=prompt_bytes,
                    round_num=state.current_round,
                    ref=ref,
                )
            )
            for hotkey in hotkeys
        ]
        results = await _join(asyncio.gather(*tasks), tasks)
        return dict(zip(hotkeys, results, strict=True))

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
            ref=ref,
        )

        prompts_to_process = [p for p in prompts if p not in generations]

        if not prompts_to_process:
            logger.info(f"Skipping {hotkey[:10]}: all prompts complete")
        else:
            logger.info(f"Processing {len(prompts_to_process)}/{len(prompts)} prompts for {hotkey[:10]}")

            # No return_exceptions: `_fetch_render_upload` catches per-prompt failures
            # internally and persists them as failure_reason. Anything escaping is a
            # `persist()` git-write failure or an unhandled bug — both warrant an
            # iteration restart rather than a silent missing-prompt entry.
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
            pending = [asyncio.create_task(task) for task in tasks]
            await _join(asyncio.gather(*pending), pending)

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

        Storage and service failures propagate for retry. Terminal delivery outcomes
        and validation rejections persist a categorical failure_reason.

        generations: Mutable dict updated in-place with results. Used as a shared state
            across concurrent tasks - each task writes its prompt's result and persists
            the entire dict, enabling crash recovery.
        """
        log_id = f"{hotkey[:10]} / {prompt}"
        template = self.settings.storage_key_template
        selected = SelectedOutput()

        def key_for(filename: str) -> str:
            return template.format(round=round_num, hotkey=hotkey, filename=filename)

        async def persist(
            *,
            js: str | None = None,
            views: str | None = None,
            size: int = 0,
            failure_reason: str | None = None,
        ) -> None:
            generations[prompt] = GenerationResult(
                js=js,
                views=views,
                size=size,
                failure_reason=failure_reason,
                source_sha256=selected.source_sha256,
                submission_revision=selected.revision,
            )
            await save_generations(
                git_batcher=self.git_batcher,
                round_num=round_num,
                hotkey=hotkey,
                generations=generations,
            )

        render_key = self.settings.render_api_key.get_secret_value() if self.settings.render_api_key else None

        if self.settings.download_jitter_seconds > 0:
            jitter = random.uniform(0, self.settings.download_jitter_seconds)  # nosec B311 # noqa: S311
            await asyncio.sleep(jitter)

        async with self.semaphore:
            # Phase 1: resolve the admitted output source.
            if prompt_image is None or self._seed is None:
                raise ArtifactReadError("Frozen generation inputs are unavailable")
            selected = await self.artifacts.selected(
                hotkey,
                submission,
                prompt_image,
                self._seed,
                self.settings.max_js_size_bytes,
            )
            js_data, fetch_failure = selected.source, selected.failure_reason
            if js_data is None:
                await persist(failure_reason=fetch_failure)
                return
            size = len(js_data)

            # Phase 2: publish the selected JavaScript.
            js_url = await _upload_to_r2(
                self.r2, key_for(f"{prompt}.js"), js_data, "application/javascript", log_id, self.settings.cdn_url
            )

            if selected.failure_reason is not None:
                await persist(js=js_url, size=size, failure_reason=selected.failure_reason)
                return

            # Phase 3: render views + grid in parallel. Any None here means a render
            # service / preview pipeline problem — distinct from miner failures.
            async def render_previews() -> tuple[dict[str, bytes] | None, dict[str, bytes] | None, bytes | None]:
                white_task = asyncio.create_task(
                    render_views(
                        client=self.http_client,
                        endpoint=self.settings.render_service_url,
                        js_content=js_data,
                        views=WHITE_VIEWS,
                        bg_color=WHITE_BG,
                        log_id=log_id,
                        api_key=render_key,
                        raise_errors=True,
                    )
                )
                gray_task = asyncio.create_task(
                    render_views(
                        client=self.http_client,
                        endpoint=self.settings.render_service_url,
                        js_content=js_data,
                        views=GRAY_VIEWS,
                        bg_color=GRAY_BG,
                        log_id=log_id,
                        api_key=render_key,
                        raise_errors=True,
                    )
                )
                grid_task = asyncio.create_task(
                    asyncio.sleep(0, result=selected.grid)
                    if selected.grid is not None
                    else render_grid(
                        client=self.http_client,
                        endpoint=self.settings.render_service_url,
                        js_content=js_data,
                        log_id=log_id,
                        api_key=render_key,
                        raise_errors=True,
                    )
                )
                return await _join(asyncio.gather(white_task, gray_task, grid_task), (white_task, gray_task, grid_task))

            async def complete_previews() -> dict[str, bytes]:
                white_result, gray_result, grid_result = await render_previews()
                if white_result is None or gray_result is None or grid_result is None:
                    raise ArtifactReadError("Judging render did not produce a complete bundle")
                return (
                    {f"white/{v.name}": white_result[v.name] for v in WHITE_VIEWS}
                    | {f"gray/{v.name}": gray_result[v.name] for v in GRAY_VIEWS}
                    | {"grid": grid_result}
                )

            try:
                white: dict[str, bytes] | None
                gray: dict[str, bytes] | None
                grid: bytes | None
                if selected.source_sha256 is None:
                    raise ArtifactReadError("Selected source has no digest")
                images = await self.artifacts.previews(
                    submission,
                    hotkey,
                    prompt_image,
                    selected.source_sha256,
                    complete_previews,
                )
                white = {v.name: images[f"white/{v.name}"] for v in WHITE_VIEWS}
                gray = {v.name: images[f"gray/{v.name}"] for v in GRAY_VIEWS}
                grid = images["grid"]
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 422:
                    raise
                await persist(js=js_url, size=size, failure_reason="render_rejected")
                return
            if white is None or gray is None or grid is None:
                raise ArtifactReadError("Judging render did not produce a complete bundle")

            # Phase 4a: embeddings.
            hf_token = self.settings.hf_token.get_secret_value() if self.settings.hf_token else None
            npz_bytes = await build_embeddings_npz(
                prompt_image, white, log_id, revision=self.settings.dinov3_revision, hf_token=hf_token
            )

            # Phase 4b: bundle upload. Atomic — every PNG and the npz upload, or none.
            bundle = (
                [(key_for(f"{prompt}/white/{view.name}.png"), white[view.name], "image/png") for view in WHITE_VIEWS]
                + [(key_for(f"{prompt}/gray/{view.name}.png"), gray[view.name], "image/png") for view in GRAY_VIEWS]
                + [
                    (key_for(f"{prompt}/grid.png"), grid, "image/png"),
                    (key_for(f"{prompt}/embeddings.npz"), npz_bytes, "application/octet-stream"),
                ]
            )
            upload_start = asyncio.get_running_loop().time()
            uploads = [
                asyncio.create_task(_upload_to_r2(self.r2, key, data, ctype, log_id, self.settings.cdn_url))
                for key, data, ctype in bundle
            ]
            await _join(asyncio.gather(*uploads), uploads)
            upload_elapsed = asyncio.get_running_loop().time() - upload_start
            bundle_kb = sum(len(data) for _, data, _ in bundle) / 1024
            logger.debug(
                f"{log_id}: uploaded {len(bundle)} files in {upload_elapsed:.1f}s, {bundle_kb:.1f}KB total"
            )

            await persist(js=js_url, views=f"{self.settings.cdn_url}/{key_for(prompt)}", size=size)


async def _upload_to_r2(r2: R2Client, key: str, data: bytes, content_type: str, log_id: str, cdn_url: str) -> str:
    """Upload data to R2 and return the CDN URL."""
    await r2.upload(key=key, data=data, content_type=content_type)
    return f"{cdn_url}/{key}"
