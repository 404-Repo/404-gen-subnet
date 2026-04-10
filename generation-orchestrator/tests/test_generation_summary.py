from subnet_common.competition.generation_report import GenerationReportOutcome
from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.generation_summary import summarize_generation
from generation_orchestrator.settings import Settings


def ok() -> GenerationResult:
    return GenerationResult(
        js="https://cdn.example.com/out.js",
        views="https://cdn.example.com/out",
    )


def fail() -> GenerationResult:
    return GenerationResult(failure_reason="test failure")


def test_all_prompts_pass(settings: Settings) -> None:
    """All submitted prompts regenerate successfully — report completed."""
    submitted_stems = {f"p{i}" for i in range(10)}
    generations = {f"p{i}": ok() for i in range(10)}

    result = summarize_generation("hk1", submitted_stems, generations, settings, total_generation_time=100.0)

    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.failed_prompts == 0
    assert result.checked_prompts == 10
    assert result.generation_time == 100.0


def test_too_many_failures_rejects(settings: Settings) -> None:
    """Exceeding max failures triggers rejection."""
    settings.max_mismatched_prompts = 2
    submitted_stems = {f"p{i}" for i in range(10)}
    generations: dict[str, GenerationResult] = {}
    for i in range(10):
        generations[f"p{i}"] = fail() if i < 4 else ok()

    result = summarize_generation("hk1", submitted_stems, generations, settings)

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert result.failed_prompts == 4
    assert "Failed prompts" in result.reason


def test_missing_regeneration_counts_as_failure(settings: Settings) -> None:
    """A submitted prompt with no regeneration at all counts as a failure."""
    settings.max_mismatched_prompts = 0
    submitted_stems = {"p0", "p1", "p2"}
    # Pod only regenerated p0; p1 and p2 never got a result (e.g., batches crashed)
    generations = {"p0": ok()}

    result = summarize_generation("hk1", submitted_stems, generations, settings)

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert result.failed_prompts == 2
    assert result.checked_prompts == 3


def test_unsubmitted_prompts_ignored(settings: Settings) -> None:
    """Generations for prompts not in submitted set are ignored — out of scope."""
    submitted_stems = {"p0"}
    generations = {"p0": ok(), "p1": fail(), "p2": fail()}

    result = summarize_generation("hk1", submitted_stems, generations, settings)

    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.checked_prompts == 1


def test_total_generation_time_too_slow_rejects(settings: Settings) -> None:
    """High total generation time triggers rejection even if all prompts succeed."""
    settings.total_generation_time_limit_seconds = 1000.0
    submitted_stems = {f"p{i}" for i in range(5)}
    generations = {f"p{i}": ok() for i in range(5)}

    result = summarize_generation("hk1", submitted_stems, generations, settings, total_generation_time=1500.0)

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert "Total generation time" in result.reason
    assert result.generation_time == 1500.0


def test_total_generation_time_within_limit_passes(settings: Settings) -> None:
    """Total generation time under the limit does not cause rejection."""
    settings.total_generation_time_limit_seconds = 2000.0
    submitted_stems = {f"p{i}" for i in range(5)}
    generations = {f"p{i}": ok() for i in range(5)}

    result = summarize_generation("hk1", submitted_stems, generations, settings, total_generation_time=500.0)

    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.generation_time == 500.0


def test_multiple_failure_reasons_combined(settings: Settings) -> None:
    """Multiple rejection reasons are combined in the reason string."""
    settings.max_mismatched_prompts = 0
    settings.total_generation_time_limit_seconds = 50.0
    submitted_stems = {f"p{i}" for i in range(4)}
    generations = {f"p{i}": fail() for i in range(4)}

    result = summarize_generation("hk1", submitted_stems, generations, settings, total_generation_time=100.0)

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert "Total generation time" in result.reason
    assert "Failed prompts" in result.reason
