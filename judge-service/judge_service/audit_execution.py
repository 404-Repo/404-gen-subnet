"""Audit duels and verdict computation for judge-service.

Two duel artifacts per audited miner — both saved as MatchReports with kind='audit':

  D1 — `audit_submitted.json`        — submitted vs generated.
       Bounds drift between what was submitted and what the solution actually produces.
       LEFT='submitted' (sentinel), RIGHT=audited hotkey, RIGHT win = legit.

  D2 — `audit_{defender[:10]}.json`  — generated vs the leader the miner beat.
       Verifies the solution genuinely beats the leader (quality).
       LEFT=defender hotkey, RIGHT=audited hotkey, RIGHT win = legit.
       Re-produced when latest_defender changes; old D2 files for previous defenders
       sit on disk as forensic data and are not consulted by `compute_verification`.

Verdict is derived, never persisted. `compute_verification` reads D1 + the D2 against
the miner's CURRENT latest_defender and returns:
    PENDING  — D1 or D2-vs-current-defender is missing.
    PASSED   — D1.margin ≥ −W AND D2.margin ≥ +W.
    FAILED   — otherwise.

The producer functions (`produce_d1_duel`, `produce_d2_duel`) are pure data — they run
the multi-stage judge over each prompt and persist a MatchReport. No verdict logic
lives in the producers. Callers in `round_runner` decide what to produce when, based
on what's already on disk.
"""

from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.generations import GenerationsMap
from subnet_common.competition.match_report import (
    MatchReport,
    get_match_report,
    save_match_report,
)
from subnet_common.competition.verification_audit import VerificationOutcome
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.match_execution import run_match


SUBMITTED_LEFT = "submitted"


async def produce_d1_duel(
    openai: AsyncOpenAI,
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    prompts: list[str],
    seed: int,
    submitted_gens: GenerationsMap,
    generated_gens: GenerationsMap,
    max_concurrent_vlm_calls: int,
    max_concurrent_duels: int,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Produce D1: submitted vs generated. Saved at rounds/{n}/{hotkey}/audit_submitted.json."""
    report = await run_match(
        openai=openai,
        prompts=prompts,
        seed=seed,
        left_gens=submitted_gens,
        right_gens=generated_gens,
        left=SUBMITTED_LEFT,
        right=hotkey,
        max_concurrent_vlm_calls=max_concurrent_vlm_calls,
        max_concurrent_duels=max_concurrent_duels,
        shutdown=shutdown,
    )
    if shutdown.should_stop:
        logger.info(f"D1 interrupted before save: {hotkey[:10]}")
        return report
    await save_match_report(git_batcher, round_num, report, kind="audit")
    logger.info(f"D1 saved: {hotkey[:10]} submitted vs generated: margin={report.margin:+.2%}")
    return report


async def produce_d2_duel(
    openai: AsyncOpenAI,
    git_batcher: GitBatcher,
    round_num: int,
    audited_hotkey: str,
    defender_hotkey: str,
    prompts: list[str],
    seed: int,
    defender_gens: GenerationsMap,
    audited_generated: GenerationsMap,
    max_concurrent_vlm_calls: int,
    max_concurrent_duels: int,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Produce D2: generated vs defender. Saved at rounds/{n}/{audited}/audit_{defender[:10]}.json.

    Re-runs are not idempotent at this layer — callers gate on `get_match_report` first
    and only invoke this when the file is actually missing (e.g., first audit, or a new
    `latest_defender` for which no D2 has been produced yet).
    """
    report = await run_match(
        openai=openai,
        prompts=prompts,
        seed=seed,
        left_gens=defender_gens,
        right_gens=audited_generated,
        left=defender_hotkey,
        right=audited_hotkey,
        max_concurrent_vlm_calls=max_concurrent_vlm_calls,
        max_concurrent_duels=max_concurrent_duels,
        shutdown=shutdown,
    )
    if shutdown.should_stop:
        logger.info(f"D2 interrupted before save: {audited_hotkey[:10]} vs {defender_hotkey[:10]}")
        return report
    await save_match_report(git_batcher, round_num, report, kind="audit")
    logger.info(f"D2 saved: {audited_hotkey[:10]} generated vs {defender_hotkey[:10]}: " f"margin={report.margin:+.2%}")
    return report


async def compute_verification(
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    latest_defender: str,
    win_margin: float,
) -> VerificationOutcome:
    """Derive the verdict from D1 + D2-vs-current-defender artifacts.

    Reads only the D2 against the CURRENT `latest_defender`. Old D2 files for previous
    defenders are not consulted — a defender change naturally re-derives the verdict
    against the new D2 (PENDING until produced, then PASSED/FAILED). Stale D2s remain
    on disk as forensic data.

    Returns:
        PENDING — D1 or D2-vs-current-defender is missing.
        PASSED  — D1.margin ≥ −W AND D2.margin ≥ +W.
        FAILED  — otherwise.
    """
    d1 = await get_match_report(git_batcher, round_num, SUBMITTED_LEFT, hotkey, kind="audit")
    if d1 is None:
        return VerificationOutcome.PENDING
    d2 = await get_match_report(git_batcher, round_num, latest_defender, hotkey, kind="audit")
    if d2 is None:
        return VerificationOutcome.PENDING
    if d1.margin >= -win_margin and d2.margin >= win_margin:
        return VerificationOutcome.PASSED
    return VerificationOutcome.FAILED
