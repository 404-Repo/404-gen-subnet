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
from typing import Literal, ParamSpec, TypedDict, TypeVar


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
) -> None:
    """Record message timing and fixed reason codes without message contents."""
    if os.environ.get("WITNESS_TIMINGS") != "1":
        return
    with suppress(Exception):
        print(json.dumps({
            "event": "witness_transport", "host": host, "exchange_id": identifier,
            "phase": phase, "timestamp_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "kind": kind, "bytes": size,
            "sequence": sequence, "reason": reason, "error_type": error_type,
            "close_code": close_code,
        }, separators=(",", ":")), file=sys.stderr, flush=True)


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
        print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


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
                print(
                    json.dumps(record, separators=(",", ":")),
                    file=sys.stderr,
                    flush=True,
                )
            except (ValueError, KeyError, TypeError):
                continue


def started() -> int:
    """Read the diagnostic clock independently of deadline test clocks."""
    return time.monotonic_ns()
