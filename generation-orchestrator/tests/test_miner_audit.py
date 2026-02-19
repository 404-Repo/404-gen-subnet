from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_audit import GenerationAuditOutcome
from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.miner_audit import audit_generations
from generation_orchestrator.settings import Settings


def ok(generation_time: float = 30.0, distance: float = 0.05) -> GenerationResult:
    return GenerationResult(
        glb="https://cdn.example.com/out.glb",
        png="https://cdn.example.com/out.png",
        generation_time=generation_time,
        distance=distance,
    )


def fail() -> GenerationResult:
    return GenerationResult()


def test_all_prompts_pass(settings: Settings) -> None:
    """All generations are fast and close — audit passes."""
    submitted = {f"p{i}": ok() for i in range(10)}
    generations = {f"p{i}": ok(generation_time=40.0) for i in range(10)}
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=["p0", "p1"])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.outcome == GenerationAuditOutcome.PASSED
    assert result.failed_prompts == 0
    assert result.failed_critical == 0
    assert result.checked_prompts == 10


def test_too_many_noncritical_mismatches_rejects(settings: Settings) -> None:
    """Exceeding max non-critical mismatches triggers rejection even with passing criticals."""
    settings.max_mismatched_prompts = 2
    submitted = {f"p{i}": ok() for i in range(10)}
    generations = {}
    for i in range(10):
        if i < 4:
            generations[f"p{i}"] = ok(distance=0.5)  # mismatches
        else:
            generations[f"p{i}"] = ok(generation_time=40.0)

    # p0 is critical but mismatched — within critical tolerance (default 1)
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=["p0"])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.outcome == GenerationAuditOutcome.REJECTED
    assert result.failed_prompts == 3  # p1, p2, p3 are non-critical mismatches
    assert result.failed_critical == 1  # p0
    assert "Non-critical mismatches" in result.reason
    assert "Critical mismatches" not in result.reason


def test_too_many_critical_mismatches_rejects(settings: Settings) -> None:
    """Exceeding max critical mismatches triggers rejection even with passing non-criticals."""
    settings.max_mismatched_critical_prompts = 1
    settings.max_mismatched_prompts = 10  # high tolerance — non-critical won't trigger
    submitted = {f"p{i}": ok() for i in range(6)}
    generations = {f"p{i}": ok(generation_time=40.0) for i in range(6)}
    generations["p0"] = ok(distance=0.5)  # critical mismatch
    generations["p1"] = ok(distance=0.5)  # critical mismatch
    generations["p5"] = ok(distance=0.5)  # non-critical mismatch (noise)

    audit_request = AuditRequest(hotkey="hk1", critical_prompts=["p0", "p1", "p2"])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.outcome == GenerationAuditOutcome.REJECTED
    assert result.failed_critical == 2
    assert result.failed_prompts == 1  # p5
    assert "Critical mismatches" in result.reason
    assert "Non-critical mismatches" not in result.reason


def test_missing_generation_counts_as_mismatch(settings: Settings) -> None:
    """A prompt in submitted but missing from generations is a mismatch."""
    submitted = {"p0": ok(), "p1": ok(), "p2": ok()}
    generations = {"p0": ok(generation_time=40.0)}
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=["p1"])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.failed_prompts == 1
    assert result.failed_critical == 1


def test_overtime_generation_counts_as_mismatch(settings: Settings) -> None:
    """A generation exceeding the timeout counts as mismatch."""
    submitted = {"p0": ok(), "p1": ok()}
    generations = {"p0": ok(generation_time=40.0), "p1": ok(generation_time=200.0)}
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=[])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.failed_prompts == 1


def test_trimmed_median_too_slow_rejects(settings: Settings) -> None:
    """High trimmed median generation time triggers rejection even if all prompts match."""
    settings.generation_median_limit_seconds = 90.0
    settings.generation_median_trim_count = 1
    submitted = {f"p{i}": ok() for i in range(5)}
    generations = {f"p{i}": ok(generation_time=100.0) for i in range(5)}
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=[])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.outcome == GenerationAuditOutcome.REJECTED
    assert "Trimmed median" in result.reason
    assert result.generation_time is not None
    assert result.generation_time > 90.0


def test_only_submitted_prompts_audited(settings: Settings) -> None:
    """Generations for prompts not in submitted are ignored."""
    submitted = {"p0": ok()}
    generations = {"p0": ok(generation_time=40.0), "extra": ok(distance=0.99)}
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=[])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.outcome == GenerationAuditOutcome.PASSED
    assert result.checked_prompts == 1


def test_multiple_failure_reasons_combined(settings: Settings) -> None:
    """Multiple rejection reasons are combined in the reason string."""
    settings.max_mismatched_prompts = 0
    settings.max_mismatched_critical_prompts = 0
    settings.generation_median_limit_seconds = 50.0
    settings.generation_median_trim_count = 1
    submitted = {f"p{i}": ok() for i in range(4)}
    generations = {f"p{i}": ok(generation_time=60.0, distance=0.5) for i in range(4)}
    audit_request = AuditRequest(hotkey="hk1", critical_prompts=["p0"])

    result = audit_generations("hk1", audit_request, submitted, generations, settings)

    assert result.outcome == GenerationAuditOutcome.REJECTED
    assert "Trimmed median" in result.reason
    assert "Non-critical" in result.reason
    assert "Critical" in result.reason
