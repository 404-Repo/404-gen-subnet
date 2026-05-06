import asyncio
from pathlib import Path

import httpx
from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelReport, DuelWinner, MatchReport
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.judges.multi_stage import evaluate_duel


WINNER_TO_SCORE: dict[DuelWinner, int] = {
    DuelWinner.RIGHT: 1,
    DuelWinner.LEFT: -1,
    DuelWinner.DRAW: 0,
    DuelWinner.SKIPPED: 0,
}


async def run_match(
    openai: AsyncOpenAI,
    prompts: list[str],
    seed: int,
    left_gens: dict[str, GenerationResult],
    right_gens: dict[str, GenerationResult],
    left: str,
    right: str,
    max_concurrent_vlm_calls: int,
    max_concurrent_duels: int,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Run all prompt duels for a match between two miners using the multi-stage judge.

    `max_concurrent_duels` bounds how many duels run at once so each duel's reference
    image and view PNGs stay resident in vLLM's prefix cache for its whole pipeline.
    `max_concurrent_vlm_calls` is a backstop on summed in-flight calls.
    """
    sem = asyncio.Semaphore(max_concurrent_vlm_calls)
    duel_sem = asyncio.Semaphore(max_concurrent_duels)
    match_label = f"{left[:10]} vs {right[:10]}"
    match_start = asyncio.get_running_loop().time()
    logger.info(
        f"match {match_label}: starting {len(prompts)} duels "
        f"(duel_concurrency={max_concurrent_duels}, vlm_call_cap={max_concurrent_vlm_calls})"
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as http:
        duels = await asyncio.gather(
            *[
                _evaluate_prompt(
                    openai=openai,
                    http=http,
                    sem=sem,
                    duel_sem=duel_sem,
                    prompt=p,
                    left_gen=left_gens.get(Path(p).stem, GenerationResult()),
                    right_gen=right_gens.get(Path(p).stem, GenerationResult()),
                    seed=seed,
                    shutdown=shutdown,
                    log_id=f"{Path(p).stem} / {match_label}",
                )
                for p in prompts
            ]
        )
    duels = sorted(duels, key=lambda d: d.name)

    score = sum(WINNER_TO_SCORE[d.winner] for d in duels)
    margin = score / len(duels) if duels else 0
    elapsed = asyncio.get_running_loop().time() - match_start
    logger.info(f"match {match_label}: done in {elapsed:.1f}s, score={score:+d}, margin={margin:+.2%}")
    return MatchReport(
        left=left,
        right=right,
        score=score,
        margin=margin,
        duels=duels,
    )


def _preview_url(gen: GenerationResult) -> str | None:
    """Grid PNG for the duel report — 4 views at a glance is more useful than just front."""
    if not gen.views:
        return None
    return f"{gen.views}/grid.png"


async def _evaluate_prompt(
    openai: AsyncOpenAI,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    duel_sem: asyncio.Semaphore,
    prompt: str,
    left_gen: GenerationResult,
    right_gen: GenerationResult,
    seed: int,
    shutdown: GracefulShutdown,
    log_id: str = "",
) -> DuelReport:
    """Run one prompt duel. Skips with SKIPPED if shutdown fires before evaluation starts.

    Acquires `duel_sem` for the entire pipeline so the duel's reference image and view
    PNGs stay hot in vLLM's prefix cache across the ~25 calls of stages 1-4. Coroutines
    queued on the sem at shutdown bail without firing any VLM calls.
    """
    stem = Path(prompt).stem
    left_url = _preview_url(left_gen)
    right_url = _preview_url(right_gen)

    duel = DuelReport(
        name=stem,
        prompt=prompt,
        left_js=left_gen.js,
        left_png=left_url,
        right_js=right_gen.js,
        right_png=right_url,
        winner=DuelWinner.SKIPPED,
    )

    async with duel_sem:
        # Re-check after acquiring the sem: shutdown may have fired while we were
        # queued behind earlier duels. Coroutines that arrive at this check post-shutdown
        # bail without making any VLM calls.
        if shutdown.should_stop:
            return duel
        try:
            winner, detail = await evaluate_duel(
                vlm=openai,
                http=http,
                sem=sem,
                prompt_url=prompt,
                left_gen=left_gen,
                right_gen=right_gen,
                seed=seed,
                log_id=log_id or stem,
            )
            duel.winner = winner
            duel.detail = detail
        except Exception as e:
            logger.exception(f"duel evaluation failed for {stem}: {e}")
        return duel
