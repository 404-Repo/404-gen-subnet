from pathlib import Path

from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.prompt_coordinator import PromptCoordinator, PromptEntry
from generation_orchestrator.prompts import Prompt


def make_entry(stem: str) -> PromptEntry:
    return PromptEntry(prompt=Prompt(stem=stem, url=f"https://cdn.example.com/{stem}.glb", path=Path(f"{stem}.glb")))


def ok(generation_time: float = 30.0, distance: float = 0.05, attempts: int = 0) -> GenerationResult:
    return GenerationResult(
        glb="https://cdn.example.com/out.glb",
        png="https://cdn.example.com/out.png",
        generation_time=generation_time,
        distance=distance,
        attempts=attempts,
    )


def fail(attempts: int = 0) -> GenerationResult:
    return GenerationResult(attempts=attempts)


def coord(
    max_attempts: int = 3,
    lock_timeout: float = 300.0,
    acceptable_distance: float = 0.1,
    median_limit: float = 90.0,
    hard_limit: float = 180.0,
    median_trim_count: int = 6,
) -> PromptCoordinator:
    return PromptCoordinator(
        max_attempts=max_attempts,
        lock_timeout=lock_timeout,
        acceptable_distance=acceptable_distance,
        median_limit=median_limit,
        hard_limit=hard_limit,
        median_trim_count=median_trim_count,
    )


def test_happy_path_all_succeed_first_try() -> None:
    """3 pods each grab a fresh prompt, all succeed → done."""
    pc = coord()
    pc.seed([make_entry("a"), make_entry("b"), make_entry("c")], {})

    for pod in ("pod-1", "pod-2", "pod-3"):
        entry, is_retry = pc.assign(pod)
        assert entry is not None
        assert not is_retry
        pc.record_result(pod, entry.prompt.stem, ok())

    assert pc.all_done()
    assert len(pc.generations) == 3
    assert pc.mismatch_count == 0


def test_failures_get_retried_then_succeed() -> None:
    """pod-1 fails prompt "a", pod-2 retries it and succeeds."""
    pc = coord(max_attempts=3)
    pc.seed([make_entry("a")], {})

    # pod-1 gets "a", fails
    e1, _ = pc.assign("pod-1")
    pc.record_result("pod-1", "a", fail())
    assert not pc.all_done()

    # pod-2 without allow_retry sees nothing — only retries remain
    e_no_retry, _ = pc.assign("pod-2")
    assert e_no_retry is None

    # pod-2 with allow_retry picks up the retry
    e2, is_retry = pc.assign("pod-2", allow_retry=True)
    assert e2 is not None
    assert e2.prompt.stem == "a"
    assert is_retry
    pc.record_result("pod-2", "a", ok(generation_time=40.0))

    assert pc.all_done()
    assert not pc.generations["a"].is_failed()


def test_overtime_gets_retried_and_improved() -> None:
    """The first attempt is overtime (200s), retry brings it down to the 50s."""
    pc = coord()
    pc.seed([make_entry("a")], {})

    pc.assign("pod-1")
    pc.record_result("pod-1", "a", ok(generation_time=200.0))
    assert not pc.all_done()

    entry, is_retry = pc.assign("pod-2", allow_retry=True)
    assert is_retry
    pc.record_result("pod-2", "a", ok(generation_time=50.0))

    assert pc.all_done()
    assert pc.generations["a"].generation_time == 50.0


def test_mixed_results_across_multiple_pods() -> None:
    """Realistic scenario: 4 prompts, 3 pods. Mix of success, failure, overtime, retry."""
    pc = coord(max_attempts=3)
    pc.seed([make_entry("a"), make_entry("b"), make_entry("c"), make_entry("d")], {})

    # Round 1: pods grab fresh prompts
    pc.assign("pod-1")  # gets a
    pc.assign("pod-2")  # gets b
    pc.assign("pod-3")  # gets c
    # d is locked out (no pod available), but has_work sees it
    assert pc.has_work("pod-4")

    pc.record_result("pod-1", "a", ok(generation_time=25.0))  # a: done
    pc.record_result("pod-2", "b", fail())  # b: needs retry
    pc.record_result("pod-3", "c", ok(generation_time=200.0))  # c: overtime, needs retry
    assert not pc.all_done()

    # Round 2: pods get fresh "d" first (priority), then retries
    e, is_retry = pc.assign("pod-1", allow_retry=True)
    assert e is not None
    assert e.prompt.stem == "d" and not is_retry
    pc.record_result("pod-1", "d", ok(generation_time=30.0))  # d: done

    # Now only retries remain. Failed "b" has priority over overtime "c"
    e, is_retry = pc.assign("pod-1", allow_retry=True)
    assert e is not None
    assert e.prompt.stem == "b" and is_retry
    pc.record_result("pod-1", "b", ok(generation_time=45.0))  # b: recovered

    e, is_retry = pc.assign("pod-1", allow_retry=True)
    assert e is not None
    assert e.prompt.stem == "c" and is_retry
    pc.record_result("pod-1", "c", ok(generation_time=60.0))  # c: improved

    assert pc.all_done()
    assert pc.mismatch_count == 0
    assert pc.generations["b"].generation_time == 45.0
    assert pc.generations["c"].generation_time == 60.0


def test_exhausted_failures_end_as_mismatches() -> None:
    """After max_attempts failures, the prompt is done but counts as a mismatch."""
    pc = coord(max_attempts=2)
    pc.seed([make_entry("a"), make_entry("b")], {})

    # "a": two pods fail it → exhausted
    pc.assign("pod-1")
    pc.record_result("pod-1", "a", fail())
    pc.assign("pod-2", allow_retry=True)
    pc.record_result("pod-2", "a", fail())

    # "b": succeeds normally
    pc.assign("pod-3")
    pc.record_result("pod-3", "b", ok())

    assert pc.all_done()
    assert pc.mismatch_count == 1
    assert pc.generations["a"].is_failed()
    assert not pc.generations["b"].is_failed()


def test_high_distance_results_are_mismatches() -> None:
    """Good generation time but high distance → done (no retry) but counted as mismatch."""
    pc = coord(acceptable_distance=0.1)
    pc.seed([make_entry("a")], {})

    pc.assign("pod-1")
    pc.record_result("pod-1", "a", ok(generation_time=30.0, distance=0.5))

    assert pc.all_done()
    assert pc.mismatch_count == 1


def test_seed_restores_partial_progress() -> None:
    """Restart mid-generation: good/done, failed, hard overtime, soft overtime all restored correctly."""
    pc = coord(median_limit=90.0, hard_limit=180.0, median_trim_count=1)
    # 7 entries so trimmed median (cut 1 each end) stays above 90 for soft overtime
    prior = {
        "done1": ok(generation_time=30.0, attempts=1),  # good → done
        "done2": ok(generation_time=95.0, attempts=1),  # slow but acceptable → done
        "done3": ok(generation_time=95.0, attempts=1),  # slow but acceptable → done
        "done4": ok(generation_time=95.0, attempts=1),  # slow but acceptable → done
        "failed": fail(attempts=1),  # failed → needs retry
        "hard_ot": ok(generation_time=200.0, attempts=1),  # hard overtime → needs retry
        "soft_ot": ok(generation_time=100.0, attempts=1),  # soft overtime → needs retry
    }
    entries = [make_entry(s) for s in prior]
    pc.seed(entries, prior)

    assert not pc.all_done()
    assert pc.generations["done1"].generation_time == 30.0

    # Failed gets priority
    e, is_retry = pc.assign("pod-1", allow_retry=True)
    assert e is not None
    assert e.prompt.stem == "failed" and is_retry
    pc.record_result("pod-1", "failed", ok(generation_time=55.0))

    # Hard overtime next
    e, is_retry = pc.assign("pod-2", allow_retry=True)
    assert e is not None
    assert e.prompt.stem == "hard_ot" and is_retry
    pc.record_result("pod-2", "hard_ot", ok(generation_time=70.0))

    # Soft overtime last — trimmed median of [30, 55, 70, 95, 95, 95, 100]
    # cut 1 each end → [55, 70, 95, 95, 95], median = 95 > 90 → eligible
    e, is_retry = pc.assign("pod-3", allow_retry=True)
    assert e is not None
    assert e.prompt.stem == "soft_ot" and is_retry
    pc.record_result("pod-3", "soft_ot", ok(generation_time=60.0))

    assert pc.all_done()
    assert pc.mismatch_count == 0


def test_pod_cannot_retry_its_own_prompt() -> None:
    """A pod that already attempted a prompt never gets it again."""
    pc = coord(max_attempts=3)
    pc.seed([make_entry("a")], {})

    pc.assign("pod-1")
    pc.record_result("pod-1", "a", ok(generation_time=200.0))

    # pod-1 already tried "a" — gets nothing even with allow_retry
    e, _ = pc.assign("pod-1", allow_retry=True)
    assert e is None
    # but work still exists — just not for pod-1
    assert not pc.all_done()
    assert pc.has_work("pod-2", allow_retry=True)
    assert not pc.has_work("pod-1", allow_retry=True)

    # pod-2 can pick it up
    e, is_retry = pc.assign("pod-2", allow_retry=True)
    assert e is not None and e.prompt.stem == "a" and is_retry


def test_locking_prevents_double_assignment() -> None:
    """While pod-1 holds a lock, pod-2 can't get the same prompt."""
    pc = coord()
    pc.seed([make_entry("a")], {})

    pc.assign("pod-1")

    e, _ = pc.assign("pod-2")
    assert e is None
    # but has_work sees through locks
    assert pc.has_work("pod-2")


def test_late_result_for_done_prompt_ignored() -> None:
    """Once a prompt is done, further results are discarded."""
    pc = coord()
    pc.seed([make_entry("a")], {})

    pc.assign("pod-1")
    pc.record_result("pod-1", "a", ok(generation_time=30.0))
    assert pc.all_done()

    updated = pc.record_result("pod-2", "a", ok(generation_time=10.0))
    assert not updated
    # original result preserved
    assert pc.generations["a"].generation_time == 30.0


def test_result_for_unknown_stem_ignored() -> None:
    pc = coord()
    pc.seed([make_entry("a")], {})

    assert not pc.record_result("pod-1", "nonexistent", ok())


def test_soft_overtime_not_retried_when_median_below_limit() -> None:
    """Soft overtime entries are skipped when the trimmed median is under median_limit."""
    pc = coord(median_limit=90.0, hard_limit=180.0, median_trim_count=6)
    # 14 entries: 13 fast + 1 soft overtime
    entries = [make_entry(f"p{i}") for i in range(14)]
    prior = {f"p{i}": ok(generation_time=30.0, attempts=1) for i in range(13)}
    prior["p13"] = ok(generation_time=100.0, attempts=1)
    pc.seed(entries, prior)

    # trimmed median of [30]*13 + [100], cut 6 each end → [30, 30] → 30 < 90
    e, _ = pc.assign("pod-1", allow_retry=True)
    assert e is None
