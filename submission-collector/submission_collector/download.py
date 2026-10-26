import asyncio
import random
from pathlib import Path

import httpx
from loguru import logger
from subnet_common.competition.generations import (
    MAX_GENERATIONS_BYTES,
    GenerationResult,
    GenerationsAdapter,
    GenerationsMap,
    get_generations,
    save_generations,
)
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.state import CompetitionState
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.git_batcher import GitBatcher

from submission_collector.settings import Settings


class DownloadPipeline:
    """Collect generated and submitted results."""

    def __init__(
        self,
        git_batcher: GitBatcher,
        http_client: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self.git_batcher = git_batcher
        self.http_client = http_client
        self.settings = settings
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)

    async def run(self, state: CompetitionState, ref: str) -> dict[str, GenerationsMap]:
        """Collect generated and submitted result records from the CDN.

        Processes submissions in random order to avoid bias.
        Supports resume - skips already completed generations.
        Saves each miner's collected index for crash recovery.

        Returns hotkey → prompt → GenerationResult for all miners.
        """
        submissions = await require_submissions(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompt_urls = await require_prompts(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompts = [Path(url).stem for url in prompt_urls]
        hotkeys = list(submissions.keys())
        random.shuffle(hotkeys)
        logger.info(f"Processing {len(hotkeys)} submissions × {len(prompts)} prompts")

        # No return_exceptions: any exception from `_download_submission` is an
        # infrastructure error (e.g. a CDN or Git failure) or an unhandled bug.
        # These must restart the iteration rather than silently count a miner as zero.
        # Missing records and reported generation/render failures carry a
        # `failure_reason` and follow the normal result path.
        tasks = [
            self._download_submission(
                hotkey=hotkey,
                submission=submissions[hotkey],
                prompts=prompts,
                round_num=state.current_round,
                ref=ref,
            )
            for hotkey in hotkeys
        ]
        results = await asyncio.gather(*tasks)
        return dict(zip(hotkeys, results, strict=True))

    async def _download_submission(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompts: list[str],
        round_num: int,
        ref: str,
    ) -> GenerationsMap:
        """Read one miner's saved index and persist the requested prompt results."""
        generations = await get_generations(
            git=self.git_batcher.git,
            round_num=round_num,
            hotkey=hotkey,
            ref=ref,
        )
        prompts_to_process = [p for p in prompts if p not in generations]
        if not prompts_to_process:
            logger.info(f"Skipping {hotkey[:10]}: all prompts complete")
            return {p: generations[p] for p in prompts if p in generations}

        logger.info(f"Processing {len(prompts_to_process)}/{len(prompts)} prompts for {hotkey[:10]}")
        if self.settings.download_jitter_seconds > 0:
            jitter = random.uniform(0, self.settings.download_jitter_seconds)  # nosec B311 # noqa: S311
            await asyncio.sleep(jitter)

        async with self.semaphore:
            key = f"rounds/{submission.round}/{hotkey}/submitted.json"
            for attempt in range(5):
                try:
                    async with self.http_client.stream("GET", f"{self.settings.cdn_url}/{key}") as response:
                        if response.status_code == 404:
                            saved: GenerationsMap = {}
                        else:
                            response.raise_for_status()
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > MAX_GENERATIONS_BYTES:
                                    raise ValueError("Result index exceeds its size limit")
                            saved = GenerationsAdapter.validate_json(raw)
                    break
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 429 or attempt == 4:
                        raise
                    delay = float(2 ** (attempt + 1))
                    try:
                        delay = max(delay, int(error.response.headers.get("Retry-After", "")))
                    except ValueError:
                        pass
                    logger.warning(f"Rate limited collecting {key}; retry {attempt + 2}/5 in {delay:g}s")
                    await asyncio.sleep(delay)
            for prompt in prompts_to_process:
                result = saved.get(prompt, GenerationResult(failure_reason="not_submitted"))
                if result.js is not None:
                    result.js = f"{self.settings.cdn_url}/{result.js}"
                if result.views is not None:
                    result.views = f"{self.settings.cdn_url}/{result.views}"
                generations[prompt] = result
            await save_generations(
                git_batcher=self.git_batcher,
                round_num=round_num,
                hotkey=hotkey,
                generations=generations,
            )
        return {p: generations[p] for p in prompts if p in generations}
