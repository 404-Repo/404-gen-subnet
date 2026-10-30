"""Optional monotonic diagnostics; never used for authorization or deadlines."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from queue import Full, Queue
from threading import Event, Lock, Thread, current_thread, get_ident, get_native_id
from typing import Literal, ParamSpec, TextIO, TypedDict, TypeVar


class _DiagnosticOutput:
    """Bound diagnostic memory and keep a stalled log sink off capture threads."""

    def __init__(self, *, capacity: int = 1024, name: str = "witness-diagnostics") -> None:
        self.queue: Queue[tuple[TextIO, str]] = Queue(maxsize=capacity)
        self.lock = Lock()
        self.thread: Thread | None = None
        self.name = name

    def write(self, value: object) -> None:
        line = json.dumps(value, separators=(",", ":"))
        with self.lock:
            if self.thread is None:
                self.thread = Thread(target=self._run, name=self.name, daemon=True)
                self.thread.start()
        with suppress(Full):
            self.queue.put_nowait((sys.stderr, line))

    def _run(self) -> None:
        while True:
            stream, line = self.queue.get()
            try:
                with suppress(Exception):
                    stream.write(line + "\n")
                    stream.flush()
            finally:
                self.queue.task_done()


_output = _DiagnosticOutput()
_stall_output = _DiagnosticOutput(capacity=128, name="witness-stall-diagnostics")


class TimingRecord(TypedDict):
    event: Literal["witness_timing"]
    schema: int
    host: str
    trace_id: str
    exchange_id: str | None
    phase: str
    start_ms: float | None
    duration_ms: float
    outcome: Literal["ok", "error"]
    error_type: str | None
    steps_ms: dict[str, float] | None
    parameters_cached: bool | None


@dataclass
class _Trace:
    started: int
    exchange_id: str | None = None
    records: list[TimingRecord] = field(default_factory=list)
    phase: str = ""
    progress_ns: int = 0
    samples: int = 0
    sampled_ns: int = 0
    thread_id: int = 0
    native_id: int = 0
    thread_name: str = ""


class _StallWatchdog:
    """Sample active worker threads without reading their locals or source lines."""

    def __init__(self, *, threshold: float = 5, interval: float = 5, poll: float = 0.25) -> None:
        self.threshold_ns, self.interval_ns = int(threshold * 1e9), int(interval * 1e9)
        self.poll = poll
        self.lock = Lock()
        self.active: dict[int, _Trace] = {}
        self.stopped = Event()
        self.thread: Thread | None = None

    def start(self, trace: _Trace) -> None:
        with self.lock:
            if self.thread is None:
                thread = Thread(target=self._run, name="witness-stall-watchdog", daemon=True)
                thread.start()
                self.thread = thread
            trace.thread_id, trace.native_id = get_ident(), get_native_id()
            trace.thread_name = current_thread().name
            self.active[trace.thread_id] = trace

    def advance(self, trace: _Trace, phase: str | None = None) -> None:
        with self.lock:
            if phase is not None:
                trace.phase = phase
            trace.progress_ns = time.monotonic_ns()
            trace.samples = trace.sampled_ns = 0

    def finish(self, trace: _Trace) -> None:
        with self.lock:
            if self.active.get(trace.thread_id) is trace:
                del self.active[trace.thread_id]

    def close(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

    def _run(self) -> None:
        while not self.stopped.wait(self.poll):
            with suppress(Exception):
                for record in self._sample():
                    _stall_output.write(record)

    def _sample(self) -> list[dict[str, object]]:
        now = time.monotonic_ns()
        candidates: list[tuple[_Trace, str, str, int, int, int]] = []
        with self.lock:
            for trace in self.active.values():
                threshold = 60_000_000_000 if trace.phase == "evidence_retry_wait" else self.threshold_ns
                if (
                    trace.exchange_id is None
                    or trace.samples >= 3
                    or now - trace.progress_ns < threshold
                    or (trace.samples and now - trace.sampled_ns < self.interval_ns)
                ):
                    continue
                trace.samples += 1
                trace.sampled_ns = now
                candidates.append((trace, trace.phase, trace.exchange_id, trace.progress_ns, trace.samples, threshold))
        records: list[dict[str, object]] = []
        for trace, phase, identifier, progress_ns, number, threshold in candidates:
            stack = self._stack(trace.thread_id)
            with self.lock:
                if (
                    not stack
                    or self.active.get(trace.thread_id) is not trace
                    or trace.phase != phase
                    or trace.exchange_id != identifier
                    or trace.progress_ns != progress_ns
                ):
                    continue
            records.append({
                "event": "witness_worker_stall", "schema": 1, "host": "worker",
                "trace_id": f"{os.getpid()}-{trace.started}", "exchange_id": identifier,
                "phase": phase, "timestamp_ns": time.time_ns(), "monotonic_ns": now,
                "no_progress_ms": (now - progress_ns) / 1e6,
                "threshold_ms": threshold / 1e6, "sample_number": number,
                "thread_id": trace.thread_id, "native_thread_id": trace.native_id,
                "thread_name": trace.thread_name, "stack": stack,
                "pod_id": os.environ.get("RUNPOD_POD_ID"),
                "pod_start_id": os.environ.get("WITNESS_START_ID"),
                "kernel_wait": self._kernel_wait(trace.native_id),
            })
        return records

    @staticmethod
    def _stack(thread_id: int) -> list[dict[str, object]]:
        frame = sys._current_frames().get(thread_id)
        stack: list[dict[str, object]] = []
        while frame is not None and len(stack) < 32:
            stack.append({
                "file": "/".join(frame.f_code.co_filename.replace("\\", "/").split("/")[-2:]),
                "function": frame.f_code.co_name, "line": frame.f_lineno,
            })
            frame = frame.f_back
        return stack

    @staticmethod
    def _kernel_wait(native_id: int) -> str | None:
        with suppress(OSError, UnicodeError), open(f"/proc/self/task/{native_id}/wchan", encoding="ascii") as stream:
            return stream.read(128).strip() or None
        return None


_watchdog = _StallWatchdog()


_trace: ContextVar[_Trace | None] = ContextVar("witness_timing", default=None)
P = ParamSpec("P")
T = TypeVar("T")
HOST = "worker"


def transport(
    phase: str,
    identifier: str,
    *,
    host: str = HOST,
    kind: str = "",
    size: int = 0,
    sequence: int = 0,
    reason: str = "",
    error_type: str | None = None,
    close_code: int | None = None,
    expected_bytes: int | None = None,
) -> None:
    """Record message timing and fixed reason codes without message contents."""
    if os.environ.get("WITNESS_TIMINGS") != "1":
        return
    progress()
    with suppress(Exception):
        _output.write({
            "event": "witness_transport", "host": host, "exchange_id": identifier,
            "phase": phase, "timestamp_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "kind": kind, "bytes": size,
            "sequence": sequence, "reason": reason, "error_type": error_type,
            "close_code": close_code,
            "expected_bytes": expected_bytes,
        })


def identify_exchange(identifier: str) -> None:
    """Correlate subsequent and enclosing spans with the allocation UUID."""
    trace = _trace.get()
    if trace is not None:
        trace.exchange_id = identifier


def progress() -> None:
    """A polling iteration or transport event proves the worker is still moving."""
    with suppress(Exception):
        trace = _trace.get()
        if trace is not None:
            _watchdog.advance(trace)


def emit(
    phase: str,
    started: int,
    *,
    error_type: str | None = None,
    duration_ms: float | None = None,
    steps_ms: dict[str, float] | None = None,
    parameters_cached: bool | None = None,
) -> None:
    """Write only timing fields; a broken diagnostic sink cannot fail an exchange.

    Native steps use their own clock and have no Python-relative start offset.
    Parent spans include their children and must not be added to them.
    """
    with suppress(Exception):
        trace = _trace.get()
        if trace is None:
            return
        native = duration_ms is not None
        record: TimingRecord = {
            "event": "witness_timing",
            "schema": 1,
            "host": HOST + (".native" if steps_ms is not None else ""),
            "trace_id": f"{os.getpid()}-{trace.started}",
            "exchange_id": trace.exchange_id,
            "phase": phase,
            "start_ms": None if native else (started - trace.started) / 1e6,
            "duration_ms": (
                duration_ms
                if duration_ms is not None
                else (time.monotonic_ns() - started) / 1e6
            ),
            "outcome": "error" if error_type else "ok",
            "error_type": error_type,
            "steps_ms": steps_ms,
            "parameters_cached": parameters_cached,
        }
        trace.records.append(record)
        _output.write(record)


def snapshot() -> list[TimingRecord]:
    """Copy completed spans for a worker's diagnostic job result."""
    trace = _trace.get()
    return list(trace.records) if trace is not None else []


@contextmanager
def measure(phase: str) -> Iterator[None]:
    """Time one operation, including failures, without recording its arguments."""
    if _trace.get() is None and os.environ.get("WITNESS_TIMINGS") != "1":
        yield
        return
    started = time.monotonic_ns()
    token = _trace.set(_Trace(started)) if _trace.get() is None else None
    trace = _trace.get()
    assert trace is not None
    previous_phase = trace.phase
    with suppress(Exception):
        _watchdog.advance(trace, phase)
        if token is not None:
            _watchdog.start(trace)
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        emit(phase, started, error_type=error_type)
        with suppress(Exception):
            if token is not None:
                _watchdog.finish(trace)
            else:
                _watchdog.advance(trace, previous_phase)
        if token is not None:
            _trace.reset(token)


def timed(phase: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Instrument an existing call without changing its signature or result."""

    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def run(*args: P.args, **kwargs: P.kwargs) -> T:
            with measure(phase):
                return function(*args, **kwargs)

        return run

    return decorate


def exchange_id() -> str:
    trace = _trace.get()
    return trace.exchange_id or "" if trace is not None else ""


def forward(stderr: bytes) -> None:
    """Retain only validated subprocess timing records, never other stderr."""
    with suppress(Exception):
        trace = _trace.get()
        if trace is None:
            return
        for line in stderr.splitlines():
            try:
                value = json.loads(line)
                if (
                    not isinstance(value, dict)
                    or value.get("event") != "witness_timing"
                    or value.get("exchange_id") != trace.exchange_id
                ):
                    continue
                # The Node emitter owns this fixed, content-free schema.
                record: TimingRecord = {
                    "event": "witness_timing",
                    "schema": 1,
                    "host": "verifier",
                    "trace_id": str(value["trace_id"]),
                    "exchange_id": trace.exchange_id,
                    "phase": str(value["phase"]),
                    "start_ms": (
                        float(value["start_ms"])
                        if value.get("start_ms") is not None
                        else None
                    ),
                    "duration_ms": float(value["duration_ms"]),
                    "outcome": "error" if value.get("outcome") == "error" else "ok",
                    "error_type": None,
                    "steps_ms": None,
                    "parameters_cached": None,
                }
                trace.records.append(record)
                _output.write(record)
            except (ValueError, KeyError, TypeError):
                continue


def started() -> int:
    """Read the diagnostic clock independently of deadline test clocks."""
    return time.monotonic_ns()
