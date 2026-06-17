from subnet_common.competition.generation_report import (
    GenerationReport,
    GenerationReportOutcome,
    RepeatStats,
)
from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.settings import Settings


def summarize_generation(
    hotkey: str,
    submitted_stems: set[str],
    generations_by_repeat: dict[int, dict[str, GenerationResult]],
    generation_time_by_repeat: dict[int, float],
    settings: Settings,
) -> GenerationReport:
    """Summarize an audit run across all repeats.

    Per repeat: count generated vs failed prompts for the submitted set. A repeat fails
    if its generation time exceeds the limit or its failures exceed tolerance. The whole
    report is REJECTED if any repeat fails; otherwise COMPLETED.
    """
    repeats: list[RepeatStats] = []
    failures: list[str] = []

    limit = settings.total_generation_time_limit_seconds
    tolerance = settings.max_mismatched_prompts

    for repeat_index in sorted(generations_by_repeat):
        generations = generations_by_repeat[repeat_index]
        generation_time = generation_time_by_repeat.get(repeat_index, 0.0)

        generated_prompts = 0
        failed_prompts = 0
        for stem in submitted_stems:
            result = generations.get(stem)
            if result is None or result.is_failed():
                failed_prompts += 1
            else:
                generated_prompts += 1

        repeats.append(
            RepeatStats(
                repeat_index=repeat_index,
                generated_prompts=generated_prompts,
                failed_prompts=failed_prompts,
                generation_time=generation_time if generation_time > 0 else None,
            )
        )

        if generation_time > limit:
            failures.append(f"Repeat {repeat_index}: time ({generation_time:.1f}s) exceeds limit ({limit:.1f}s)")
        if failed_prompts > tolerance:
            failures.append(f"Repeat {repeat_index}: {failed_prompts} failed prompts exceed tolerance ({tolerance})")

    return GenerationReport(
        hotkey=hotkey,
        outcome=GenerationReportOutcome.REJECTED if failures else GenerationReportOutcome.COMPLETED,
        repeats=repeats,
        reason="; ".join(failures),
    )
