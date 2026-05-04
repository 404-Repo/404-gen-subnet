"""Verification duel: submitted vs generated for one audited hotkey.

For each prompt, run the multi-stage judge with submitted as left, generated as right.
Score per prompt:
    -1  submitted won  (miner's solution can't reproduce its own submission — suspicious)
    +1  generated won  (solution reproduces or exceeds submission — legitimate)
     0  draw or skipped

DRAW means the judge ran end-to-end and ruled the two sides equivalent (or both sides
had no previews). DRAW contributes 0 to score and is reported under `drawn` in the
audit summary — it does not count as a "decided" duel.

SKIPPED is the per-duel default in `DuelReport` and only persists when the judge
raised an unhandled exception that we caught (logged at audit_execution.py with
`audit duel failed for <stem>`). It shouldn't happen in normal operation; if it does,
a WARNING is emitted so an operator notices. SKIPPED is intentionally not surfaced as
a separate field on `VerificationAudit` — chasing it via logs is the right entry point.

Sum >= 0 → PASSED; otherwise FAILED. The asymmetry catches cheating: a miner who
submits offline-generated / cherry-picked assets but deploys a weaker reproducer
will have submitted dominate generated, score goes negative, audit fails.

Same `evaluate_duel` engine as miner-vs-miner matches, same `MatchReport` artifact
(saved as `duels_submitted.json` — `left="submitted"` matches the per-duel left side,
mirroring how miner-vs-miner files name the file after the actual left), plus a slim
`VerificationAudit` summary for fast resume / lookup.
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


SUBMITTED_LEFT = "submitted"


def _preview_url(gen: GenerationResult) -> str | None:
    """Grid PNG for the duel report — 4 views at a glance is more useful than just front."""
    if not gen.views:
        return None
    return f"{gen.views}/grid.png"


_SCORE_FOR: dict[DuelWinner, int] = {
    DuelWinner.LEFT: -1,  # submitted won — miner couldn't reproduce; suspicious
    DuelWinner.RIGHT: 1,  # generated won — solution reproduces or exceeds; legitimate
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
    LEFT win = -1 (submitted dominated, suspicious), RIGHT win = +1 (generated held up,
    legitimate), DRAW or SKIPPED = 0. Sum >= 0 → PASSED.
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
    decided = sum(1 for d in duels if d.winner in (DuelWinner.LEFT, DuelWinner.RIGHT))
    drawn = sum(1 for d in duels if d.winner == DuelWinner.DRAW)
    skipped = sum(1 for d in duels if d.winner == DuelWinner.SKIPPED)
    total = len(duels)
    # `checked` = duels the judge ran to a verdict (decisive or draw). SKIPPED is a
    # judge-side crash, so it's excluded from "checked". In normal operation skipped=0
    # and checked == total.
    checked = decided + drawn
    if skipped:
        # Shouldn't happen in normal operation — evaluate_duel handles missing previews
        # internally and only SKIPPED leaks through on shutdown or unhandled exception.
        # Log loudly so an operator notices instead of letting it hide in the duels file.
        logger.warning(f"audit {hotkey[:10]}: {skipped} duel(s) returned SKIPPED — judge crashed for those stems")

    # Reuse MatchReport to keep the artifact shape identical to miner-vs-miner duels.
    # `left=SUBMITTED_LEFT`/`right=hotkey` makes the saved path `rounds/{n}/{hotkey}/duels_submitted.json`.
    # The label matches the per-duel `left_js=submitted.js` data, so a reader sees a
    # consistent "left = submitted, right = the audited miner's regenerated output".
    report = MatchReport(
        left=SUBMITTED_LEFT,
        right=hotkey,
        score=score,
        margin=score / len(duels) if duels else 0,
        duels=duels,
    )
    await save_match_report(git_batcher, round_num, report)

    breakdown = f"{checked} checked ({decided} decided, {drawn} drawn)"

    if shutdown.should_stop:
        # Don't finalize a verdict on shutdown — it'll resume next iteration with the
        # saved partial report (next pass will see no audit yet and re-run).
        return VerificationAudit(
            hotkey=hotkey,
            outcome=VerificationOutcome.PENDING,
            score=score,
            total_prompts=total,
            checked_prompts=checked,
            drawn=drawn,
            reason="interrupted by shutdown",
        )

    if score >= 0:
        outcome = VerificationOutcome.PASSED
        reason = f"score={score} ≥ 0 over {breakdown}"
    else:
        outcome = VerificationOutcome.FAILED
        reason = f"score={score} < 0 over {breakdown}"

    elapsed = asyncio.get_running_loop().time() - audit_start
    logger.info(f"audit {hotkey[:10]}: {outcome.value} in {elapsed:.1f}s " f"(score={score}, {breakdown})")
    return VerificationAudit(
        hotkey=hotkey,
        outcome=outcome,
        score=score,
        total_prompts=total,
        checked_prompts=checked,
        drawn=drawn,
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
