"""Timing cannot disclose arguments, alter errors, or extend business deadlines."""

from __future__ import annotations

import json
from io import StringIO
from typing import cast

import pytest
from witness_client import timing


def records(capsys: pytest.CaptureFixture[str]) -> list[timing.TimingRecord]:
    return [
        cast(timing.TimingRecord, json.loads(line))
        for line in capsys.readouterr().err.splitlines()
    ]


def test_timings_are_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("WITNESS_TIMINGS", raising=False)
    with timing.measure("disabled"):
        timing.identify_exchange("test-exchange")
    timing.transport("send_start", "test-exchange", kind="binary", size=42)
    assert records(capsys) == []


def test_transport_records_only_message_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    timing.transport("send_end", "test-exchange", kind="handshake", size=42, sequence=3)
    event = json.loads(capsys.readouterr().err)
    assert event["event"] == "witness_transport"
    assert (event["host"], event["exchange_id"], event["kind"]) == ("client", "test-exchange", "handshake")
    assert (event["bytes"], event["sequence"]) == (42, 3)
    assert event["timestamp_ns"] > 0 and event["monotonic_ns"] > 0
    assert event["error_type"] is None


def test_monotonic_nested_spans_and_failure_privacy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    clock = [1_000_000_000]
    monkeypatch.setattr("witness_client.timing.time.monotonic_ns", lambda: clock[0])
    monkeypatch.setattr("witness_client.timing.time.time", lambda: -1_000_000)
    failure = ValueError("private token, prompt, and response")
    with pytest.raises(ValueError) as caught, timing.measure("create"):
        timing.identify_exchange("test-exchange")
        clock[0] += 2_000_000
        with timing.measure("proof"):
            clock[0] += 7_000_000
            raise failure
    assert caught.value is failure
    output = records(capsys)
    assert [(r["phase"], r["duration_ms"], r["start_ms"]) for r in output] == [
        ("proof", 7, 2),
        ("create", 9, 0),
    ]
    assert {r["exchange_id"] for r in output} == {"test-exchange"}
    assert len({r["trace_id"] for r in output}) == 1
    assert all(r["error_type"] == "ValueError" for r in output)
    assert "private token" not in json.dumps(output)
    with timing.measure("next_call"):
        pass
    assert records(capsys)[0]["exchange_id"] is None


class BrokenSink(StringIO):
    def write(self, value: str) -> int:
        raise OSError("Diagnostic destination unavailable")


def test_broken_sink_preserves_result_and_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    monkeypatch.setattr("witness_client.timing.sys.stderr", BrokenSink())
    timing.transport("send_end", "test-exchange")

    @timing.timed("operation")
    def operation(value: int) -> int:
        if value < 0:
            raise ValueError("original failure")
        return value * 2

    assert operation(3) == 6
    with pytest.raises(ValueError, match="original failure"):
        operation(-1)
