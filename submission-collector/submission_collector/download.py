import asyncio
import random
from pathlib import Path

import httpx
from loguru import logger
from subnet_common.competition.generations import (
    GenerationResult,
    GenerationsAdapter,
    GenerationsMap,
    save_generations,
)
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.state import CompetitionState
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.git_batcher import GitBatcher
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt

from submission_collector.settings import Settings


def _retry_delay(retry_state: RetryCallState) -> float:
    delay = 3.0
    error = retry_state.outcome.exception() if retry_state.outcome is not None else None
    if isinstance(error, httpx.HTTPStatusError):
        try:
            delay = max(delay, int(error.response.headers.get("Retry-After", "")))
        except ValueError:
            pass
    return delay


class ResultCollector:
    """Collect miners' saved result indexes."""

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

    async def collect(self, state: CompetitionState, ref: str) -> dict[str, GenerationsMap]:
        """Read each miner's complete index and save results for the round's prompts."""
        submissions = await require_submissions(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompt_urls = await require_prompts(git=self.git_batcher.git, round_num=state.current_round, ref=ref)
        prompts = [Path(url).stem for url in prompt_urls]
        hotkeys = list(submissions.keys())
        random.shuffle(hotkeys)
        logger.info(f"Collecting {len(hotkeys)} result indexes for {len(prompts)} prompts")

        tasks = [
            self._collect_miner(
                hotkey=hotkey,
                submission=submissions[hotkey],
                prompts=prompts,
                round_num=state.current_round,
            )
            for hotkey in hotkeys
        ]
        results = await asyncio.gather(*tasks)
        return dict(zip(hotkeys, results, strict=True))

    async def _collect_miner(
        self,
        hotkey: str,
        submission: MinerSubmission,
        prompts: list[str],
        round_num: int,
    ) -> GenerationsMap:
        """Fetch one miner's index and replace its collected results in Git."""
        if self.settings.download_jitter_seconds > 0:
            jitter = random.uniform(0, self.settings.download_jitter_seconds)  # nosec B311 # noqa: S311
            await asyncio.sleep(jitter)

        key = f"rounds/{submission.round}/{hotkey}/submitted.json"
        saved = await self._fetch_index(key)
        generations = {}
        for prompt in prompts:
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
        return generations

    @retry(
        stop=stop_after_attempt(2),
        wait=_retry_delay,
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError)),
    )
    async def _fetch_index(self, key: str) -> GenerationsMap:
        """Fetch and parse a miner's saved result index, retrying HTTP failures."""
        async with self.semaphore:
            response = await self.http_client.get(f"{self.settings.cdn_url}/{key}")
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return GenerationsAdapter.validate_json(response.content)
