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


def _gens(stems: list[str], outcome_map: dict[str, GenerationResult] | None = None) -> dict[str, GenerationResult]:
    outcome_map = outcome_map or {}
    return {stem: outcome_map.get(stem, ok()) for stem in stems}


def test_all_prompts_pass_all_repeats(settings: Settings) -> None:
    """Three repeats, all clean — report completed with three RepeatStats entries."""
    submitted_stems = {f"p{i}" for i in range(10)}
    gens_by_repeat = {r: _gens([f"p{i}" for i in range(10)]) for r in (1, 2, 3)}
    time_by_repeat = {1: 100.0, 2: 110.0, 3: 95.0}

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat=time_by_repeat,
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert len(result.repeats) == 3
    assert [r.repeat_index for r in result.repeats] == [1, 2, 3]
    assert all(r.generated_prompts == 10 and r.failed_prompts == 0 for r in result.repeats)
    assert [r.generation_time for r in result.repeats] == [100.0, 110.0, 95.0]


def test_too_many_failures_in_one_repeat_rejects(settings: Settings) -> None:
    """Any single repeat exceeding tolerance rejects the whole report."""
    settings.max_mismatched_prompts = 2
    submitted_stems = {f"p{i}" for i in range(10)}
    bad = {f"p{i}": fail() for i in range(4)}
    gens_by_repeat = {
        1: _gens([f"p{i}" for i in range(10)]),
        2: _gens([f"p{i}" for i in range(10)], outcome_map=bad),
        3: _gens([f"p{i}" for i in range(10)]),
    }

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat={1: 50.0, 2: 60.0, 3: 55.0},
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert "Repeat 2" in result.reason
    assert result.repeats[1].failed_prompts == 4


def test_missing_regeneration_counts_as_failure(settings: Settings) -> None:
    """A submitted prompt with no regeneration counts as a failure."""
    settings.max_mismatched_prompts = 0
    submitted_stems = {"p0", "p1", "p2"}
    gens_by_repeat = {1: {"p0": ok()}}

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat={1: 10.0},
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert result.repeats[0].failed_prompts == 2
    assert result.repeats[0].generated_prompts == 1


def test_unsubmitted_prompts_ignored(settings: Settings) -> None:
    """Generations for prompts not in the submitted set are ignored."""
    submitted_stems = {"p0"}
    gens_by_repeat = {1: {"p0": ok(), "p1": fail(), "p2": fail()}}

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat={1: 10.0},
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.repeats[0].generated_prompts == 1
    assert result.repeats[0].failed_prompts == 0


def test_one_repeat_too_slow_rejects(settings: Settings) -> None:
    """If any repeat exceeds total_generation_time_limit, reject."""
    settings.total_generation_time_limit_seconds = 1000.0
    submitted_stems = {f"p{i}" for i in range(5)}
    gens_by_repeat = {r: _gens([f"p{i}" for i in range(5)]) for r in (1, 2, 3)}

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat={1: 500.0, 2: 1500.0, 3: 800.0},
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert "Repeat 2: time" in result.reason
    assert result.repeats[1].generation_time == 1500.0


def test_all_repeats_within_limit_passes(settings: Settings) -> None:
    settings.total_generation_time_limit_seconds = 2000.0
    submitted_stems = {f"p{i}" for i in range(5)}
    gens_by_repeat = {r: _gens([f"p{i}" for i in range(5)]) for r in (1, 2, 3)}

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat={1: 500.0, 2: 600.0, 3: 700.0},
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.COMPLETED


def test_multiple_failure_reasons_combined(settings: Settings) -> None:
    """Multiple rejection reasons across repeats are combined."""
    settings.max_mismatched_prompts = 0
    settings.total_generation_time_limit_seconds = 50.0
    submitted_stems = {f"p{i}" for i in range(4)}
    bad = {f"p{i}": fail() for i in range(4)}
    gens_by_repeat = {1: _gens([f"p{i}" for i in range(4)], outcome_map=bad)}

    result = summarize_generation(
        "hk1",
        submitted_stems,
        generations_by_repeat=gens_by_repeat,
        generation_time_by_repeat={1: 100.0},
        settings=settings,
    )

    assert result.outcome == GenerationReportOutcome.REJECTED
    assert "Repeat 1: time" in result.reason
    assert "Repeat 1:" in result.reason and "failed prompts" in result.reason
