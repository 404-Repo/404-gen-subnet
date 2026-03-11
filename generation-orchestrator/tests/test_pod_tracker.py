from generation_orchestrator.pod_tracker import PodTracker


def tracker(
    min_samples: int = 8,
    failure_threshold: int = 8,
    warmup_half_window: int = 4,
    rolling_median_limit: float = 120.0,
    hard_limit: float = 180.0,
    retry_delta_min_samples: int = 3,
) -> PodTracker:
    return PodTracker(
        min_samples=min_samples,
        failure_threshold=failure_threshold,
        warmup_half_window=warmup_half_window,
        rolling_median_limit=rolling_median_limit,
        hard_limit=hard_limit,
        retry_delta_min_samples=retry_delta_min_samples,
    )


def test_healthy_pod_processes_all_prompts() -> None:
    """A pod that generates everything quickly is never terminated."""
    pt = tracker(min_samples=8)

    for _ in range(20):
        pt.record(success=True, generation_time=50.0)

    should_term, reason = pt.should_terminate()
    assert not should_term
    assert pt.processed == 20
    assert pt.successful == 20
    assert pt.hard_failed == 0
    assert pt.hard_overtime == 0


def test_failures_accumulate_and_trigger_termination() -> None:
    """Pod with too many hard failures gets terminated."""
    pt = tracker(min_samples=8, failure_threshold=8)

    # 8 failures reach the threshold
    for _ in range(8):
        pt.record(success=False, generation_time=0.0)

    should_term, reason = pt.should_terminate()
    assert should_term
    assert "failures" in reason
    assert pt.hard_failed == 8


def test_overtimes_count_toward_termination() -> None:
    """Hard overtime results combine with failures toward the threshold."""
    pt = tracker(min_samples=8, failure_threshold=8, hard_limit=180.0)

    # 4 failures + 4 overtimes = 8 bad total
    for _ in range(4):
        pt.record(success=False, generation_time=0.0)
    for _ in range(4):
        pt.record(success=True, generation_time=200.0)

    should_term, reason = pt.should_terminate()
    assert should_term
    assert "4 failures" in reason and "4 overtimes" in reason


def test_not_terminated_before_min_samples() -> None:
    """Even with all failures, the pod isn't terminated until min_samples processed."""
    pt = tracker(min_samples=8, failure_threshold=4)

    for _ in range(7):
        pt.record(success=False, generation_time=0.0)

    should_term, _ = pt.should_terminate()
    assert not should_term

    # More pushes past min_samples
    pt.record(success=False, generation_time=0.0)
    should_term, _ = pt.should_terminate()
    assert should_term


def test_slow_pod_terminated_when_not_warming_up() -> None:
    """Pod with high rolling median and no warmup trend gets terminated."""
    pt = tracker(min_samples=8, rolling_median_limit=120.0, warmup_half_window=4)

    # 8 slow samples — all above the rolling median limit, no downward trend
    for _ in range(8):
        pt.record(success=True, generation_time=130.0)

    should_term, reason = pt.should_terminate()
    assert should_term
    assert "rolling median" in reason


def test_slow_pod_spared_during_warmup() -> None:
    """Pod with a high median but downward trend (warming up) is not terminated."""
    pt = tracker(min_samples=8, rolling_median_limit=120.0, warmup_half_window=4)

    # 4 very slow, then 4 slightly less slow — downward trend
    for _ in range(4):
        pt.record(success=True, generation_time=160.0)
    for _ in range(4):
        pt.record(success=True, generation_time=130.0)

    assert pt.is_warmup_trending_down()
    should_term, _ = pt.should_terminate()
    assert not should_term


def test_warmup_trend_needs_enough_samples() -> None:
    """Warmup trend detection returns False when fewer than 2*half_window samples exist."""
    pt = tracker(warmup_half_window=4)

    # 7 samples with a clear downward trend — but need 8 (2*4) for detection
    for t in [160.0, 155.0, 150.0, 145.0, 140.0, 135.0, 130.0]:
        pt.record(success=True, generation_time=t)

    assert not pt.is_warmup_trending_down()


def test_marked_bad_always_terminates() -> None:
    """Once marked bad, should_terminate returns True regardless of stats."""
    pt = tracker()
    pt.mark_pod_bad("pod-1", "external reason")

    should_term, reason = pt.should_terminate()
    assert should_term
    assert "already marked bad" in reason
    assert pt.termination_reason == "external reason"


def test_mark_bad_is_idempotent() -> None:
    """Calling mark_pod_bad twice doesn't overwrite the first reason."""
    pt = tracker()
    pt.mark_pod_bad("pod-1", "first reason")
    pt.mark_pod_bad("pod-1", "second reason")

    assert pt.termination_reason == "first reason"


def test_retries_accepted_during_warmup() -> None:
    """Retries are always accepted before min_samples successes."""
    pt = tracker(min_samples=8)

    for _ in range(4):
        pt.record(success=True, generation_time=50.0)
    # 3 retries that are slower — but total successful (4+3=7) < min_samples, so still warmup
    for _ in range(3):
        pt.record(success=True, generation_time=100.0, is_retry=True, original_time=50.0)

    assert pt.successful == 7
    assert pt.should_accept_retries()


def test_retries_accepted_with_insufficient_retry_data() -> None:
    """After warmup, retries are still accepted if fewer than retry_delta_min_samples recorded."""
    pt = tracker(min_samples=8, retry_delta_min_samples=3)

    for _ in range(8):
        pt.record(success=True, generation_time=50.0)

    # Only 2 retries (slower) — not enough data to reject
    for _ in range(2):
        pt.record(success=True, generation_time=80.0, is_retry=True, original_time=50.0)

    assert pt.retry_delta_count == 2
    assert pt.retry_cumulative_delta > 0
    assert pt.should_accept_retries()


def test_retries_rejected_when_not_helping() -> None:
    """After warmup, retries are rejected if cumulative delta is positive (slower)."""
    pt = tracker(min_samples=8, retry_delta_min_samples=3)

    # Warmup: 8 successes
    for _ in range(8):
        pt.record(success=True, generation_time=50.0)

    # 3 retries that are all slower: delta = +30 each
    for _ in range(3):
        pt.record(success=True, generation_time=80.0, is_retry=True, original_time=50.0)

    assert not pt.should_accept_retries()
    assert pt.retry_cumulative_delta == 90.0


def test_retries_accepted_when_helping() -> None:
    """After warmup, retries are accepted if the cumulative delta is negative (faster)."""
    pt = tracker(min_samples=8, retry_delta_min_samples=3)

    for _ in range(8):
        pt.record(success=True, generation_time=100.0)

    # 3 retries that are faster: delta = -40 each
    for _ in range(3):
        pt.record(success=True, generation_time=60.0, is_retry=True, original_time=100.0)

    assert pt.should_accept_retries()
    assert pt.retry_cumulative_delta == -120.0


def test_retry_samples_excluded_from_performance() -> None:
    """Retry generation times don't pollute the performance_samples used for rolling median."""
    pt = tracker(min_samples=8)

    for _ in range(8):
        pt.record(success=True, generation_time=50.0)
    # Retry with very high time — should NOT affect rolling median
    pt.record(success=True, generation_time=500.0, is_retry=True, original_time=50.0)

    assert pt.rolling_median_performance() == 50.0


def test_rolling_median_uses_last_window() -> None:
    """Rolling median only considers the last N samples."""
    pt = tracker(min_samples=8)

    # 8 slow samples, then 8 fast samples
    for _ in range(8):
        pt.record(success=True, generation_time=150.0)
    for _ in range(8):
        pt.record(success=True, generation_time=40.0)

    assert pt.rolling_median_performance(window=8) == 40.0


def test_rolling_median_none_before_enough_samples() -> None:
    """Rolling median returns None when not enough samples."""
    pt = tracker()

    for _ in range(7):
        pt.record(success=True, generation_time=50.0)

    assert pt.rolling_median_performance(window=8) is None


def test_to_stats_snapshot() -> None:
    """to_stats produces a correct PodStats snapshot."""
    pt = tracker()

    pt.record(success=True, generation_time=40.0)
    pt.record(success=True, generation_time=60.0)
    pt.record(success=False, generation_time=0.0)
    pt.record(success=True, generation_time=200.0)  # hard overtime
    pt.record(success=True, generation_time=50.0, is_retry=True, original_time=80.0)

    stats = pt.to_stats("pod-1")
    assert stats.pod_id == "pod-1"
    assert stats.processed == 5
    assert stats.successful == 3  # 40, 60, and the 50 retry (all under hard_limit)
    assert stats.hard_failed == 1
    assert stats.hard_overtime == 1
    assert stats.time_min == 40.0
    assert stats.time_max == 60.0
    assert stats.retry_cumulative_delta == -30.0
    assert stats.retry_delta_count == 1
