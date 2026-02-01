import asyncio
from pathlib import Path

from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelReport, DuelWinner, MatchReport
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.evaluate_duel import evaluate_duel


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
    max_concurrent_duels: int,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Run all prompt duels for a match between two miners.

    Evaluates each prompt concurrently (limited by max_concurrent_duels),
    aggregates scores, and returns the complete match report.
    """
    sem = asyncio.Semaphore(max_concurrent_duels)

    duels = await asyncio.gather(
        *[
            _evaluate_prompt(
                openai=openai,
                sem=sem,
                prompt=p,
                left_gen=left_gens.get(Path(p).stem, GenerationResult()),
                right_gen=right_gens.get(Path(p).stem, GenerationResult()),
                seed=seed,
                shutdown=shutdown,
            )
            for p in prompts
        ]
    )
    duels = sorted(duels, key=lambda d: d.name)

    score = sum(WINNER_TO_SCORE[d.winner] for d in duels)
    margin = score / len(duels) if duels else 0
    return MatchReport(
        left=left,
        right=right,
        score=score,
        margin=margin,
        duels=duels,
    )


async def _evaluate_prompt(
    openai: AsyncOpenAI,
    sem: asyncio.Semaphore,
    prompt: str,
    left_gen: GenerationResult,
    right_gen: GenerationResult,
    seed: int,
    shutdown: GracefulShutdown,
) -> DuelReport:
    """Evaluate a single prompt duel between left and right miners.

    Returns early with the appropriate winner if previews are missing or
    generation time penalties apply. Otherwise, calls the judge VLM.
    """
    stem = Path(prompt).stem

    duel = DuelReport(
        name=stem,
        prompt=prompt,
        left_glb=left_gen.glb,
        left_png=left_gen.png,
        right_glb=right_gen.glb,
        right_png=right_gen.png,
        winner=DuelWinner.SKIPPED,
        issues="Preview is missing",
    )

    if result := _check_missing_previews(left_gen, right_gen, stem):
        duel.winner, duel.issues = result
        return duel

    left_url = left_gen.png
    right_url = right_gen.png
    if not left_url or not right_url:
        return duel  # unreachable after _check_missing_previews

    async with sem:
        if shutdown.should_stop:
            return duel

        start = asyncio.get_running_loop().time()
        duel_result = await evaluate_duel(
            client=openai,
            prompt_url=prompt,
            left_url=left_url,
            right_url=right_url,
            seed=seed,
        )
        elapsed = asyncio.get_running_loop().time() - start
        logger.debug(f"Duel {stem}: {duel_result.outcome:+d} in {elapsed:.1f}s")

        duel.winner = _outcome_to_winner(duel_result.outcome)
        duel.issues = duel_result.issues
        return duel


def _check_missing_previews(
    left_gen: GenerationResult,
    right_gen: GenerationResult,
    stem: str,
) -> tuple[DuelWinner, str] | None:
    """Return winner based on missing previews, or None if both present."""
    if not left_gen.png and not right_gen.png:
        logger.debug(f"No previews for {stem}")
        return DuelWinner.DRAW, f"No previews for {stem}"

    if not left_gen.png:
        logger.debug(f"No preview for {stem} from left")
        return DuelWinner.RIGHT, f"No preview for {stem} from left"

    if not right_gen.png:
        logger.debug(f"No preview for {stem} from right")
        return DuelWinner.LEFT, f"No preview for {stem} from right"

    return None


def _outcome_to_winner(outcome: int) -> DuelWinner:
    """Convert numeric outcome to DuelWinner."""
    if outcome < 0:
        return DuelWinner.LEFT
    if outcome > 0:
        return DuelWinner.RIGHT
    return DuelWinner.DRAW
