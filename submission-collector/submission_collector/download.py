import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx
from loguru import logger
from subnet_common.competition.config import require_competition_config
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
from subnet_common.git_batcher import GitBatcher
from subnet_common.r2_client import R2Client

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
    """Collect selected outputs and completed judging bundles saved by the Pod."""

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
        self.artifacts: WitnessArtifacts | None = None
        self._seed: int | None = None

    async def run(self, state: CompetitionState, ref: str) -> dict[str, GenerationsMap]:
        """Collect saved sources and judging-bundle URLs from R2.

        Processes submissions in random order to avoid bias.
        Supports resume - skips already completed generations.
        Saves progress after each generation for crash recovery.

        Returns hotkey → prompt → GenerationResult for all miners.
        """
        submissions = await require_submissions(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompt_urls = await require_prompts(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompts = [Path(url).stem for url in prompt_urls]
        self._seed = await require_seed_from_git(self.git_batcher.git, state.current_round, ref)
        config = await require_competition_config(git=self.git_batcher.git, ref=ref)
        round_name = f"{config.name}-{state.current_round}"
        self.artifacts = await WitnessArtifacts.for_round(self.r2, round_name)
        # Read each current-round prompt once to verify the saved input binding.
        prompt_bytes = await self._fetch_prompt_images(prompt_urls)
        if any(prompt not in prompt_bytes for prompt in prompts):
            raise ArtifactReadError("A frozen prompt image is unavailable")
        actual_hashes = {hashlib.sha256(image).hexdigest() for image in prompt_bytes.values()}
        prompt_set_hash = hashlib.sha256(json.dumps(sorted(actual_hashes), separators=(",", ":")).encode()).hexdigest()
        if (
            self._seed != self.artifacts.round.seed
            or prompt_set_hash != self.artifacts.round.prompt_hashes_sha256
            or len(actual_hashes) != len(prompts)
            or any(s.round != round_name for s in submissions.values())
        ):
            raise ArtifactReadError("Saved inputs differ from the frozen current round")

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

            # Terminal outcomes are saved as data. Storage, incomplete-bundle and
            # git failures restart the iteration so pending outputs remain retryable.
            tasks = [
                self._collect_prompt(
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

    async def _collect_prompt(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompt: str,
        prompt_image: bytes | None,
        round_num: int,
        generations: GenerationsMap,
    ) -> None:
        """Verify the Pod's completed bundle and persist its URLs for judging."""
        selected = SelectedOutput()

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

        if self.settings.download_jitter_seconds > 0:
            jitter = random.uniform(0, self.settings.download_jitter_seconds)  # nosec B311 # noqa: S311
            await asyncio.sleep(jitter)

        async with self.semaphore:
            if prompt_image is None or self._seed is None or self.artifacts is None:
                raise ArtifactReadError("Current round inputs are unavailable")
            selected = await self.artifacts.selected(
                hotkey, submission, prompt_image, self._seed, self.settings.max_js_size_bytes
            )
            cdn = self.settings.cdn_url
            await persist(
                js=f"{cdn}/{selected.source_key}" if selected.source_key else None,
                views=f"{cdn}/{selected.views_key}" if selected.views_key else None,
                size=len(selected.source) if selected.source is not None else 0,
                failure_reason=selected.failure_reason,
            )
