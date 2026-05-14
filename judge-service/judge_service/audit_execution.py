"""Audit duel producers for judge-service.

Per audited miner, both audits are produced once per generation repeat (audit_repeats):

  Submitted audit — `audit_submitted_{r}.json` — submitted vs generated_{r}.
       Bounds drift between what was submitted and what the solution actually produces.
       LEFT='submitted' (sentinel), RIGHT=audited hotkey, RIGHT win = legit.

  Defender audit — `audit_{defender[:10]}_{r}.json` — generated_{r} vs defender.
       Verifies the solution genuinely beats the leader the miner beat.
       LEFT=defender hotkey, RIGHT=audited hotkey, RIGHT win = legit.

Margins are indexed in `audit_matrix.csv` under composite left keys (`submitted_{r}`,
`{defender}_{r}`); verdict computation in `round_runner` reads the matrix in memory
rather than the per-duel reports. Full reports are kept on disk for forensics only.
"""

from loguru import logger
from openai import AsyncOpenAI
from subnet_common.competition.generations import GenerationsMap
from subnet_common.competition.match_report import MatchReport, save_match_report
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.match_execution import run_match


async def produce_generated_vs_submitted_audit(
    openai: AsyncOpenAI,
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    prompts: list[str],
    seed: int,
    submitted_gens: GenerationsMap,
    generated_gens: GenerationsMap,
    repeat_index: int,
    max_concurrent_vlm_calls: int,
    max_concurrent_duels: int,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Run submitted vs generated for one repeat. Saved at rounds/{n}/{hotkey}/audit_submitted_{r}.json."""
    report = await run_match(
        openai=openai,
        prompts=prompts,
        seed=seed,
        left_gens=submitted_gens,
        right_gens=generated_gens,
        left="submitted",
        right=hotkey,
        max_concurrent_vlm_calls=max_concurrent_vlm_calls,
        max_concurrent_duels=max_concurrent_duels,
        shutdown=shutdown,
    )
    if shutdown.should_stop:
        logger.info(f"submitted duel interrupted before save: {hotkey[:10]} r{repeat_index}")
        return report
    await save_match_report(git_batcher, round_num, report, kind="audit", repeat_index=repeat_index)
    logger.info(
        f"submitted duel saved: {hotkey[:10]} submitted vs generated r{repeat_index}: margin={report.margin:+.2%}"
    )
    return report


async def produce_generated_vs_defender_audit(
    openai: AsyncOpenAI,
    git_batcher: GitBatcher,
    round_num: int,
    audited_hotkey: str,
    defender_hotkey: str,
    prompts: list[str],
    seed: int,
    defender_gens: GenerationsMap,
    audited_generated: GenerationsMap,
    repeat_index: int,
    max_concurrent_vlm_calls: int,
    max_concurrent_duels: int,
    shutdown: GracefulShutdown,
) -> MatchReport:
    """Run generated vs defender for one repeat.

    Saved at rounds/{n}/{audited}/audit_{defender[:10]}_{r}.json. Re-runs are not
    idempotent at this layer — callers gate on the audit matrix and only invoke this
    when the corresponding margin is missing.
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
        logger.info(
            f"defender duel interrupted before save: {audited_hotkey[:10]} vs {defender_hotkey[:10]} r{repeat_index}"
        )
        return report
    await save_match_report(git_batcher, round_num, report, kind="audit", repeat_index=repeat_index)
    logger.info(
        f"defender duel saved: {audited_hotkey[:10]} generated vs {defender_hotkey[:10]} r{repeat_index}: "
        f"margin={report.margin:+.2%}"
    )
    return report
