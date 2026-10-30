"""Optional monotonic diagnostics; never used for authorization or deadlines."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from queue import Full, Queue
from threading import Lock, Thread
from typing import Literal, ParamSpec, TextIO, TypedDict, TypeVar


class _GatewayDiagnosticOutput:
    """Keep blocked diagnostic output off the gateway event loop with bounded memory."""

    def __init__(self) -> None:
        self.queue: Queue[tuple[TextIO, str]] = Queue(maxsize=1024)
        self.lock = Lock()
        self.thread: Thread | None = None

    def write(self, value: object) -> None:
        line = json.dumps(value, separators=(",", ":"))
        with self.lock:
            if self.thread is None:
                self.thread = Thread(target=self._run, name="witness-gateway-diagnostics", daemon=True)
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


_gateway_output = _GatewayDiagnosticOutput()


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


_trace: ContextVar[_Trace | None] = ContextVar("witness_timing", default=None)
P = ParamSpec("P")
T = TypeVar("T")
HOST = "client"


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
    with suppress(Exception):
        record = {
            "event": "witness_transport", "host": host, "exchange_id": identifier,
            "phase": phase, "timestamp_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "kind": kind, "bytes": size,
            "sequence": sequence, "reason": reason, "error_type": error_type,
            "close_code": close_code,
            "expected_bytes": expected_bytes,
        }
        if host == "gateway":
            _gateway_output.write(record)
        else:
            print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


def identify_exchange(identifier: str) -> None:
    """Correlate subsequent and enclosing spans with the allocation UUID."""
    trace = _trace.get()
    if trace is not None:
        trace.exchange_id = identifier


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
            "host": HOST + (".native" if native else ""),
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
        print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


@contextmanager
def measure(phase: str) -> Iterator[None]:
    """Time one operation, including failures, without recording its arguments."""
    if _trace.get() is None and os.environ.get("WITNESS_TIMINGS") != "1":
        yield
        return
    started = time.monotonic_ns()
    token = _trace.set(_Trace(started)) if _trace.get() is None else None
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        emit(phase, started, error_type=error_type)
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
