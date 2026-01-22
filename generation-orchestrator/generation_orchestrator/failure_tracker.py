import statistics

from loguru import logger
from pydantic import BaseModel, Field


class FailureTracker(BaseModel):
    """Per-worker statistics for bad pod detection."""

    # Thresholds (set at init, don't change)
    min_samples: int  # Min processed before checking bad pod (8)
    failure_threshold: int  # Max failures before bad (8)
    median_limit: float  # Soft limit for trimmed median (90s)
    hard_limit: float  # Hard limit - counts as failure (180s)
    acceptable_distance: float  # Max acceptable distance for preview match (0.1)

    # Counters (updated during processing)
    processed: int = 0
    successful: int = 0  # Generations that completed and generation time < hard_limit
    failed: int = 0  # Generations that failed or generation time >= hard_limit
    mismatched: int = 0  # Generations that don't match submitted results (distance > acceptable)
    successful_times: list[float] = Field(default_factory=list)  # Times of successful generations
    bad_pod_logged: bool = False

    def record(self, success: bool, generation_time: float | None, distance: float) -> None:
        """Record a generation result."""
        self.processed += 1

        if not success or (generation_time is not None and generation_time >= self.hard_limit):
            self.failed += 1
            return

        self.successful += 1
        if generation_time is not None:
            self.successful_times.append(generation_time)

        if distance > self.acceptable_distance:
            self.mismatched += 1

    def trimmed_median_generation_time(self) -> float | None:
        """
        Calculate trimmed median of successful generation times.
        Removes 1 lowest and 1 highest value.
        Returns None if < 3 samples (can't trim meaningfully).
        """
        if len(self.successful_times) < 3:
            return None
        sorted_times = sorted(self.successful_times)
        return statistics.median(sorted_times[1:-1])

    def is_pod_bad(self) -> tuple[bool, str]:
        """
        Check if the pod should be terminated based on failure criteria.
        Returns: (is_bad, reason).
        """
        if self.processed < self.min_samples:
            return False, ""

        total_failures = self.failed + self.mismatched
        if total_failures >= self.failure_threshold:
            return True, f"{self.failed} failed + {self.mismatched} mismatched >= {self.failure_threshold}"

        if self.successful >= self.min_samples:
            med = self.trimmed_median_generation_time()
            if med is not None and med > self.median_limit:
                return True, f"trimmed median {med:.1f}s > {self.median_limit}s"

        return False, ""

    def log_bad_pod_once(self, worker_id: str, reason: str) -> None:
        """Log a pod failure once."""
        if self.bad_pod_logged:
            return
        self.bad_pod_logged = True
        logger.warning(f"{worker_id} is bad: {reason}")

    def effective_timeout(self) -> float:
        """
        Return the effective timeout threshold based on pod performance.

        Returns hard_limit if the pod has been fast (trimmed median <= median_limit),
        otherwise median_limit to avoid accepting slow generations from slow pods.
        """
        trimmed_med = self.trimmed_median_generation_time()
        if trimmed_med is not None and trimmed_med <= self.median_limit:
            return self.hard_limit
        return self.median_limit
