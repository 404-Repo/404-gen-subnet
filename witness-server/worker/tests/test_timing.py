"""Worker diagnostics contain no transcript or subprocess error material."""

import json

import pytest
import timing


def test_transport_failure_records_stage_without_error_contents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    timing.transport("capture_failed", "fixture-id", reason="provider_handshake_read", error_type="ValueError")
    event = json.loads(capsys.readouterr().err)
    assert (event["host"], event["exchange_id"]) == ("worker", "fixture-id")
    assert (event["reason"], event["error_type"]) == ("provider_handshake_read", "ValueError")


def test_only_timing_records_are_retained(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    with timing.measure("job"):
        timing.identify_exchange("fixture-id")
        raw = {
            "event": "witness_timing",
            "exchange_id": "fixture-id",
            "trace_id": "fixture",
            "phase": "authorize.native_proof_verify",
            "duration_ms": 5.2,
            "start_ms": None,
            "outcome": "ok",
        }
        timing.forward(b"private subprocess error\n" + json.dumps(raw).encode())
        assert timing.snapshot()[0]["duration_ms"] == 5.2
        assert timing.snapshot()[0]["start_ms"] is None
        timing.forward(b'{"event":"witness_timing","exchange_id":"other"}')
        assert len(timing.snapshot()) == 1
    output = capsys.readouterr().err
    assert "private subprocess error" not in output
    assert len(output.splitlines()) == 2
