"""Verification duel: submitted vs generated for one audited hotkey.

For each prompt, run the multi-stage judge with submitted as left, generated as right.
Score per prompt:
    +1  submitted won
    -1  generated won
     0  draw or skipped (e.g. preview missing on either side)

Sum >= 0 → PASSED; otherwise FAILED. Same `evaluate_duel` engine as miner-vs-miner
matches, same `MatchReport` artifact (saved as `duels_generated.json`), plus a
slim `VerificationAudit` summary for fast resume / lookup.
"""

import asyncio
from pathlib import Path

import httpx
from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelReport, DuelWinner, MatchReport, save_match_report
from subnet_common.competition.verification_audit import VerificationAudit, VerificationOutcome
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.judges.multi_stage import evaluate_duel


GENERATED_LEFT = "generated"


def _preview_url(gen: GenerationResult) -> str | None:
    """Grid PNG for the duel report — 4 views at a glance is more useful than just front."""
    if not gen.views:
        return None
    return f"{gen.views}/grid.png"


_SCORE_FOR: dict[DuelWinner, int] = {
    DuelWinner.LEFT: 1,
    DuelWinner.RIGHT: -1,
    DuelWinner.DRAW: 0,
    DuelWinner.SKIPPED: 0,
}


async def run_verification_audit(
    openai: AsyncOpenAI,
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    prompts: list[str],
    seed: int,
    submitted_gens: dict[str, GenerationResult],
    generated_gens: dict[str, GenerationResult],
    max_concurrent_vlm_calls: int,
    shutdown: GracefulShutdown,
) -> VerificationAudit:
    """Run submitted-vs-generated duels for one hotkey, persist report + audit, return verdict.

    Submitted is the LEFT side, generated is the RIGHT side. Per-prompt:
    LEFT win = +1, RIGHT win = -1, DRAW or SKIPPED = 0. Sum >= 0 → PASSED.
    """
    sem = asyncio.Semaphore(max_concurrent_vlm_calls)
    audit_label = f"audit/{hotkey[:10]}"
    audit_start = asyncio.get_running_loop().time()
    logger.info(f"audit {hotkey[:10]}: starting {len(prompts)} verification duels")

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as http:
        duels = await asyncio.gather(
            *[
                _evaluate_audit_prompt(
                    openai=openai,
                    http=http,
                    sem=sem,
                    prompt=p,
                    submitted=submitted_gens.get(Path(p).stem, GenerationResult()),
                    generated=generated_gens.get(Path(p).stem, GenerationResult()),
                    seed=seed,
                    shutdown=shutdown,
                    log_id=f"{Path(p).stem} / {audit_label}",
                )
                for p in prompts
            ]
        )
    duels = sorted(duels, key=lambda d: d.name)

    score = sum(_SCORE_FOR[d.winner] for d in duels)
    checked = sum(1 for d in duels if d.winner in (DuelWinner.LEFT, DuelWinner.RIGHT))

    # Reuse MatchReport to keep the artifact shape identical to miner-vs-miner duels.
    # `left=GENERATED_LEFT`/`right=hotkey` makes the saved path `rounds/{n}/{hotkey}/duels_generated.json`.
    report = MatchReport(
        left=GENERATED_LEFT,
        right=hotkey,
        score=score,
        margin=score / len(duels) if duels else 0,
        duels=duels,
    )
    await save_match_report(git_batcher, round_num, report)

    if shutdown.should_stop:
        # Don't finalize a verdict on shutdown — it'll resume next iteration with the
        # saved partial report (next pass will see no audit yet and re-run).
        return VerificationAudit(
            hotkey=hotkey,
            outcome=VerificationOutcome.PENDING,
            score=score,
            checked_prompts=checked,
            reason="interrupted by shutdown",
        )

    if score >= 0:
        outcome = VerificationOutcome.PASSED
        reason = f"submitted score={score} ≥ 0 over {checked} decided prompts"
    else:
        outcome = VerificationOutcome.FAILED
        reason = f"submitted score={score} < 0 over {checked} decided prompts"

    elapsed = asyncio.get_running_loop().time() - audit_start
    logger.info(
        f"audit {hotkey[:10]}: {outcome.value} in {elapsed:.1f}s "
        f"(score={score}, decided={checked})"
    )
    return VerificationAudit(
        hotkey=hotkey,
        outcome=outcome,
        score=score,
        checked_prompts=checked,
        reason=reason,
    )


async def _evaluate_audit_prompt(
    openai: AsyncOpenAI,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    prompt: str,
    submitted: GenerationResult,
    generated: GenerationResult,
    seed: int,
    shutdown: GracefulShutdown,
    log_id: str = "",
) -> DuelReport:
    """One audit duel. Mirrors `match_execution._evaluate_prompt` shape so reports are uniform."""
    stem = Path(prompt).stem
    left_url = _preview_url(submitted)
    right_url = _preview_url(generated)

    duel = DuelReport(
        name=stem,
        prompt=prompt,
        left_js=submitted.js,
        left_png=left_url,
        right_js=generated.js,
        right_png=right_url,
        winner=DuelWinner.SKIPPED,
    )

    if shutdown.should_stop:
        return duel

    try:
        winner, detail = await evaluate_duel(
            vlm=openai,
            http=http,
            sem=sem,
            prompt_url=prompt,
            left_gen=submitted,
            right_gen=generated,
            seed=seed,
            log_id=log_id or stem,
        )
    except Exception as e:
        logger.exception(f"audit duel failed for {stem}: {e}")
        return duel

    duel.winner = winner
    duel.detail = detail
    return duel
