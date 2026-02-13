import asyncio
import statistics
import time

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from subnet_common.competition.generations import GenerationResult

from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.prompts import Prompt


class PromptEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    prompt: Prompt  # The prompt to generate a 3D model from
    submitted_png: str | None = None  # CDN URL of submitted preview (audit mode only)

    # Attempt tracking (managed by coordinator on record_result)
    attempts: int = 0  # Total completed attempts across all pods
    attempted_by: set[str] = Field(default_factory=set)  # Short IDs that returned a result

    # Exclusive lock — one pod works this prompt at a time
    locked_by: str | None = None  # Short ID currently holding the lock
    lock_expires: float = 0.0  # Monotonic timestamp when lock auto-expires

    # Result tracking (managed by coordinator on record_result)
    best_result: GenerationResult | None = None  # Best result so far (ranked by tier, then time)
    done: bool = False  # Terminal: acceptable result achieved or max_attempts exhausted


class PromptCoordinator:
    """Assigns prompts to workers and tracks best generation results.

    Workers (4 per pod) request tasks. Prompts are locked per pod so
    only one pod works a prompt at a time.

    Assignment priority:
      1. Fresh (0 attempts)
      2. Failed (no usable output)
      3. Hard overtime (≥hard_limit)
      4. Soft overtime (>median_limit) — only when trimmed median exceeds median_limit

    Failures and missing results are capped at hard_limit for median calculation.
    Trimmed median cuts 6 from each end to ignore outliers.

    A prompt is done when it has an acceptable result (not failed,
    within distance, within median_limit), is mismatched, or exhausts max_attempts.
    """

    def __init__(
        self,
        max_attempts: int,
        lock_timeout: float,
        acceptable_distance: float,
        median_limit: float,
        hard_limit: float,
        median_trim_count: int,
    ):
        self._max_attempts = max_attempts  # Max attempts per prompt before marking done (e.g. 3)
        self._lock_timeout = lock_timeout  # Seconds before a pod's lock auto-expires (e.g. 300)
        self._acceptable_distance = acceptable_distance  # Max distance to consider a result good (e.g. 0.1)
        self._median_limit = median_limit  # Soft overtime threshold in seconds (e.g. 90)
        self._hard_limit = hard_limit  # Hard overtime / effective time for failures (e.g. 180)
        self._median_trim_count = median_trim_count  # Entries to cut from each end for trimmed median (e.g. 6)

        self._entries: dict[str, PromptEntry] = {}  # stem → entry
        self._change_event: asyncio.Event = asyncio.Event()  # Wakes waiters on state change
        self._mismatch_limit_logged: bool = False

    def seed(self, entries: list[PromptEntry], prior_results: dict[str, GenerationResult]) -> None:
        """Load entries and restore state from prior results (restart continuity)."""
        self._entries = {e.prompt.stem: e for e in entries}
        for stem, entry in self._entries.items():
            prior = prior_results.get(stem)
            if prior is not None:
                entry.best_result = prior
                entry.attempts = prior.attempts
                entry.done = not self._needs_retry(prior)

    def assign(self, short_id: str, allow_retry: bool = False) -> tuple[PromptEntry | None, bool]:
        """Assign the highest-priority available prompt to a pod.

        Returns (entry, is_retry). Entry is None if nothing is available.
        """
        entry, is_retry = self._find(short_id, allow_retry, ignore_locks=False)
        if entry:
            self._lock(entry, short_id)
            logger.debug(f"{short_id}: assigned {entry.prompt.stem} ({self._assignment_reason(entry, is_retry)})")
        return entry, is_retry

    def has_work(self, short_id: str, allow_retry: bool = False) -> bool:
        """Whether assign() would return an entry if no locks existed.

        Same logic as assign(), ignoring locks. Use to distinguish
        'nothing available right now' from 'nothing will ever be available'.
        """
        entry, _ = self._find(short_id, allow_retry, ignore_locks=True)
        return entry is not None

    def record_result(self, short_id: str, stem: str, result: GenerationResult) -> bool:
        """Record a generation result. Returns True if the entry was updated (caller should save)."""
        entry = self._entries.get(stem)
        if entry is None:
            return False

        self._unlock(entry)

        if entry.done:
            logger.debug(f"{short_id}: ignoring late result for {stem}")
            self._notify()
            return False

        entry.attempts += 1
        best = self._update_best(entry, result)
        entry.done = not self._needs_retry(best)

        self._notify()
        return True

    @property
    def generations(self) -> dict[str, GenerationResult]:
        return {s: e.best_result for s, e in self._entries.items() if e.best_result is not None}

    @property
    def mismatch_count(self) -> int:
        """Prompts that will hurt the score: high distance, or exhausted failures/hard overtimes."""
        count = 0
        for e in self._entries.values():
            r = e.best_result
            if r is None:
                continue
            if not r.is_failed() and r.distance > self._acceptable_distance:
                count += 1
            elif (r.is_failed() or r.generation_time >= self._hard_limit) and e.attempts >= self._max_attempts:
                count += 1
        return count

    def log_mismatch_limit_once(self, short_id: str) -> None:
        if not self._mismatch_limit_logged:
            self._mismatch_limit_logged = True
            logger.warning(f"{short_id}: mismatch limit reached ({self.mismatch_count})")

    def all_done(self) -> bool:
        entry, _ = self._find(short_id="all-done-check", allow_retry=True, ignore_locks=True)
        return entry is None

    async def wait_for_change(self, stop: GenerationStop) -> bool:
        """Block until state changes or stop is signaled.

        Returns True on change, False on stop.
        """
        return await stop.wait_for_event(self._change_event)

    def __len__(self) -> int:
        return len(self._entries)

    def _assignment_reason(self, entry: PromptEntry, is_retry: bool) -> str:
        if not is_retry:
            return "fresh"
        r = entry.best_result
        if r is None or r.is_failed():
            return "fail retry"
        if r.generation_time >= self._hard_limit:
            return "overtime retry"
        return "median retry"

    def _find(self, short_id: str, allow_retry: bool, ignore_locks: bool) -> tuple[PromptEntry | None, bool]:
        """Shared selection logic for assign() and has_work().

        Priority: fresh → failed → hard overtime → soft overtime.
        Soft overtime is only eligible when the trimmed median exceeds median_limit.
        """
        failed = []
        hard_overtime = []
        soft_overtime = []

        for entry in self._entries.values():
            if entry.done:
                continue
            if short_id in entry.attempted_by:
                continue
            if not ignore_locks and self._is_locked(entry):
                continue

            if entry.attempts == 0:
                return entry, False

            if not allow_retry:
                continue

            r = entry.best_result
            if r is None or r.is_failed():
                failed.append(entry)
            elif r.generation_time >= self._hard_limit:
                hard_overtime.append(entry)
            elif r.generation_time > self._median_limit:
                soft_overtime.append(entry)

        for bucket in (failed, hard_overtime):
            if bucket:
                chosen = min(bucket, key=lambda e: e.attempts)
                return chosen, True

        if soft_overtime and self._trimmed_median() > self._median_limit:
            chosen = max(soft_overtime, key=lambda e: e.best_result.generation_time)  # type: ignore[union-attr]
            return chosen, True

        return None, False

    def _trimmed_median(self) -> float:
        """Trimmed median of all generation times. Failures and missing capped at hard_limit."""
        times = sorted(self._effective_time(e) for e in self._entries.values())
        trim = self._median_trim_count
        if len(times) > 2 * trim:
            times = times[trim:-trim]
        return statistics.median(times)

    def _effective_time(self, entry: PromptEntry) -> float:
        """Generation time capped at hard_limit. Failures and missing treated as hard_limit."""
        r = entry.best_result
        if r is None or r.is_failed():
            return self._hard_limit
        return min(r.generation_time, self._hard_limit)

    def _needs_retry(self, result: GenerationResult) -> bool:
        """Result that needs retry."""
        return result.distance <= self._acceptable_distance and result.needs_retry(self._median_limit, self._max_attempts)

    def _update_best(self, entry: PromptEntry, new: GenerationResult) -> GenerationResult:
        """Keep the best result. Any non-failed beats failed. Lower time wins among non-failed."""
        old = entry.best_result
        if old is None or self._is_better(new, old):
            entry.best_result = new
        entry.best_result.attempts = entry.attempts
        return entry.best_result

    def _is_better(self, new: GenerationResult, old: GenerationResult) -> bool:
        if new.is_failed():
            return False
        if old.is_failed():
            return True
        return new.generation_time < old.generation_time

    def _is_locked(self, entry: PromptEntry) -> bool:
        return entry.locked_by is not None and time.monotonic() < entry.lock_expires

    def _lock(self, entry: PromptEntry, short_id: str) -> None:
        entry.locked_by = short_id
        entry.lock_expires = time.monotonic() + self._lock_timeout
        entry.attempted_by.add(short_id)

    def _unlock(self, entry: PromptEntry) -> None:
        entry.locked_by = None
        entry.lock_expires = 0.0

    def _notify(self) -> None:
        self._change_event.set()
        self._change_event = asyncio.Event()
