import statistics

from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_audit import GenerationAudit, GenerationAuditOutcome
from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.settings import Settings


def audit_generations(
    hotkey: str,
    audit_request: AuditRequest,
    submitted: dict[str, GenerationResult],
    generations: dict[str, GenerationResult],
    settings: Settings,
) -> GenerationAudit:
    """
    Audit generation results and determine a pass / reject verdict.
    Only audits prompts present in submitted.
    """
    critical_set = set(audit_request.critical_prompts)

    checked_prompts = 0
    mismatched_non_critical = 0
    mismatched_critical = 0
    generation_times: list[float] = []

    for stem in submitted:
        is_critical = stem in critical_set
        result = generations.get(stem)
        checked_prompts += 1

        if result is None:
            if is_critical:
                mismatched_critical += 1
            else:
                mismatched_non_critical += 1
            continue

        if not result.is_failed():
            generation_times.append(result.generation_time)

        mismatched = (
            result.is_failed()
            or result.is_overtime(settings.generation_timeout_seconds)
            or result.distance > settings.acceptable_distance
        )

        if mismatched:
            if is_critical:
                mismatched_critical += 1
            else:
                mismatched_non_critical += 1

    trimmed_median = _compute_trimmed_median(generation_times, settings.generation_median_trim_count)

    failures: list[str] = []
    if trimmed_median is not None and trimmed_median > settings.generation_median_limit_seconds:
        failures.append(
            f"Trimmed median generation time ({trimmed_median:.2f}s) "
            f"exceeds limit ({settings.generation_median_limit_seconds:.2f}s)"
        )
    if mismatched_non_critical > settings.max_mismatched_prompts:
        failures.append(
            f"Non-critical mismatches ({mismatched_non_critical}) "
            f"exceed tolerance ({settings.max_mismatched_prompts})"
        )
    if mismatched_critical > settings.max_mismatched_critical_prompts:
        failures.append(
            f"Critical mismatches ({mismatched_critical}) "
            f"exceed tolerance ({settings.max_mismatched_critical_prompts})"
        )

    return GenerationAudit(
        hotkey=hotkey,
        outcome=GenerationAuditOutcome.REJECTED if failures else GenerationAuditOutcome.PASSED,
        checked_prompts=checked_prompts,
        failed_prompts=mismatched_non_critical,
        failed_critical=mismatched_critical,
        generation_time=trimmed_median,
        reason="; ".join(failures),
    )


def _compute_trimmed_median(times: list[float], trim_count: int) -> float | None:
    """Compute median after trimming outliers from both ends."""
    if not times:
        return None

    times_sorted = sorted(times)
    if len(times_sorted) > 2 * trim_count:
        times_sorted = times_sorted[trim_count:-trim_count]

    return statistics.median(times_sorted) if times_sorted else None
