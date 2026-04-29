from subnet_common.competition.generation_report import GenerationReport, GenerationReportOutcome
from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.settings import Settings


def summarize_generation(
    hotkey: str,
    submitted_stems: set[str],
    generations: dict[str, GenerationResult],
    settings: Settings,
    total_generation_time: float = 0.0,
) -> GenerationReport:
    """Summarize generation for only the prompts the miner submitted for the round.

    A miner wins by claiming N prompts; this checks they can reproduce
    those same N. Prompts outside the submitted set are out of scope.
    A missing regeneration (no row) or a permanent miner failure counts as a failure.
    Orchestrator-side render failures (js present, png missing) are not counted here —
    the judge service surfaces those at match time.
    """
    checked_prompts = 0
    failed_prompts = 0

    for stem in submitted_stems:
        checked_prompts += 1
        result = generations.get(stem)
        if result is None or result.is_failed():
            failed_prompts += 1

    failures: list[str] = []
    if total_generation_time > settings.total_generation_time_limit_seconds:
        failures.append(
            f"Total generation time ({total_generation_time:.1f}s) "
            f"exceeds limit ({settings.total_generation_time_limit_seconds:.1f}s)"
        )
    if failed_prompts > settings.max_mismatched_prompts:
        failures.append(f"Failed prompts ({failed_prompts}) exceed tolerance ({settings.max_mismatched_prompts})")

    return GenerationReport(
        hotkey=hotkey,
        outcome=GenerationReportOutcome.REJECTED if failures else GenerationReportOutcome.COMPLETED,
        checked_prompts=checked_prompts,
        failed_prompts=failed_prompts,
        generation_time=total_generation_time if total_generation_time > 0 else None,
        reason="; ".join(failures),
    )
