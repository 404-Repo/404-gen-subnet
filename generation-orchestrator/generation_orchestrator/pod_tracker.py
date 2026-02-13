import statistics

from loguru import logger
from pydantic import BaseModel, Field
from subnet_common.competition.pod_stats import PodStats


class PodTracker(BaseModel):
    """Per-worker statistics for pod health and retry decisions."""

    # Thresholds (set at init, don't change)
    min_samples: int  # Min samples before checking pod health (8)
    failure_threshold: int  # Max combined hard failures + overtimes before termination (8)
    warmup_half_window: int  # Half-window for warmup trend detection (4)
    rolling_median_limit: float  # Rolling median bar for should_terminate (120s)
    hard_limit: float  # Hard limit - counts as hard_overtime (180s)
    retry_delta_min_samples: int  # Min retries before checking delta (3)

    # Counters (updated during processing)
    processed: int = 0
    successful: int = 0  # Generations that completed with generation time < hard_limit
    hard_failed: int = 0  # Generations that produced no output (crash, HTTP error)
    hard_overtime: int = 0  # Generations that completed but took >= hard_limit
    performance_samples: list[float] = Field(default_factory=list)  # Generation times from fresh (non-retry) prompts
    marked_bad: bool = False
    termination_reason: str = "stopped"

    # Retry tracking
    retry_cumulative_delta: float = 0.0  # Sum of (retry_time - original_time) across retries
    retry_delta_count: int = 0  # Number of retry deltas recorded

    def record(
        self, success: bool, generation_time: float, is_retry: bool = False, original_time: float | None = None
    ) -> None:
        """Record a generation result."""
        self.processed += 1

        if not success:
            self.hard_failed += 1
            return

        if generation_time >= self.hard_limit:
            self.hard_overtime += 1
            return

        self.successful += 1

        if not is_retry:
            self.performance_samples.append(generation_time)

        if is_retry and original_time is not None:
            self.retry_cumulative_delta += generation_time - original_time
            self.retry_delta_count += 1

    def rolling_median_performance(self, window: int = 8) -> float | None:
        """Median of the last `window` performance samples."""
        if len(self.performance_samples) < window:
            return None
        return statistics.median(self.performance_samples[-window:])

    def is_warmup_trending_down(self) -> bool:
        """
        Check if recent performance samples show a downward trend.
        Compares median of recent half-window vs previous half-window.
        Returns True if the recent median is lower (pod is warming up).
        """
        w = self.warmup_half_window
        if len(self.performance_samples) < w * 2:
            return False
        prev = self.performance_samples[-w * 2 : -w]
        last = self.performance_samples[-w:]
        return statistics.median(last) < statistics.median(prev)

    def should_terminate(self) -> tuple[bool, str]:
        """
        Check if the pod should be terminated based on failure criteria.
        Returns: (should_terminate, reason).
        """
        if self.marked_bad:
            return True, "already marked bad"

        if self.processed < self.min_samples:
            return False, ""

        bad_total = self.hard_failed + self.hard_overtime
        if bad_total >= self.failure_threshold:
            return True, f"{self.hard_failed} failures + {self.hard_overtime} overtimes >= {self.failure_threshold}"

        if self.successful >= self.min_samples:
            med = self.rolling_median_performance()
            if med is not None and med > self.rolling_median_limit:
                if not self.is_warmup_trending_down():
                    return True, f"rolling median {med:.1f}s > {self.rolling_median_limit}s"

        return False, ""

    def should_accept_retries(self) -> bool:
        """Accept retries until we have enough evidence that they aren't helping.
        Requires pod to be past warmup (min_samples successes) so cold-start times don't skew deltas.
        """
        if self.successful < self.min_samples:
            return True
        if self.retry_delta_count < self.retry_delta_min_samples:
            return True
        return self.retry_cumulative_delta <= 0

    def to_stats(self, pod_id: str) -> PodStats:
        """Create a PodStats snapshot from the tracker's counter fields."""
        return PodStats(
            pod_id=pod_id,
            processed=self.processed,
            successful=self.successful,
            hard_failed=self.hard_failed,
            hard_overtime=self.hard_overtime,
            performance_samples=list(self.performance_samples),
            marked_bad=self.marked_bad,
            retry_cumulative_delta=self.retry_cumulative_delta,
            retry_delta_count=self.retry_delta_count,
            termination_reason=self.termination_reason,
        )

    def mark_pod_bad(self, short_id: str, reason: str) -> None:
        """Mark pod as bad and log (idempotent)."""
        if self.marked_bad:
            return
        self.marked_bad = True
        self.termination_reason = reason
        logger.warning(f"{short_id} is bad: {reason}")
