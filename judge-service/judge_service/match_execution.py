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
    overtime_tolerance_ratio: float,
    max_generation_time_seconds: float,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Run all prompt duels for a match between two miners.

    Evaluates each prompt concurrently (limited by max_concurrent_duels),
    aggregates scores, and returns the complete match report.
    """
    sem = asyncio.Semaphore(max_concurrent_duels)
    overtime_allowance = int(len(prompts) * overtime_tolerance_ratio)

    left_overtime = _get_overtime_prompts(prompts, left_gens, overtime_allowance, max_generation_time_seconds)
    right_overtime = _get_overtime_prompts(prompts, right_gens, overtime_allowance, max_generation_time_seconds)

    duels = await asyncio.gather(
        *[
            _evaluate_prompt(
                openai=openai,
                sem=sem,
                prompt=p,
                left_gen=left_gens.get(Path(p).stem, GenerationResult()),
                right_gen=right_gens.get(Path(p).stem, GenerationResult()),
                left_overtime=Path(p).stem in left_overtime,
                right_overtime=Path(p).stem in right_overtime,
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
    left_overtime: bool,
    right_overtime: bool,
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

    if result := _check_overtime(left_overtime, right_overtime):
        duel.winner, duel.issues = result
        return duel

    left_url = left_gen.png
    right_url = right_gen.png
    if not left_url or not right_url:  # pragma: no cover
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


def _check_overtime(
    left_overtime: bool,
    right_overtime: bool,
) -> tuple[DuelWinner, str] | None:
    """Return winner based on overtime penalties, or None if no penalty."""
    if left_overtime and right_overtime:
        return DuelWinner.DRAW, "Generation time exceeded for both"

    if left_overtime:
        return DuelWinner.RIGHT, "Generation time exceeded for left"

    if right_overtime:
        return DuelWinner.LEFT, "Generation time exceeded for right"

    return None


def _outcome_to_winner(outcome: int) -> DuelWinner:
    """Convert numeric outcome to DuelWinner."""
    if outcome < 0:
        return DuelWinner.LEFT
    if outcome > 0:
        return DuelWinner.RIGHT
    return DuelWinner.DRAW


def _get_overtime_prompts(
    prompts: list[str],
    gens: dict[str, GenerationResult],
    allowance: int,
    max_generation_time_seconds: float,
) -> set[str]:
    """Return prompt stems that exceed overtime allowance.

    The first `allowance` overtime prompts are free; the rest are penalized.
    """
    penalized: set[str] = set()
    overtime_count = 0

    for p in prompts:
        stem = Path(p).stem
        gen = gens.get(stem)
        if gen and gen.generation_time and gen.generation_time > max_generation_time_seconds:
            overtime_count += 1
            if overtime_count > allowance:
                penalized.add(stem)

    return penalized
