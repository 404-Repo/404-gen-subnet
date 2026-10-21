"""Worker diagnostics contain no transcript or subprocess error material."""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from conftest import connected_channels

import timing


def test_transport_failure_records_stage_without_error_contents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    timing.transport("capture_failed", "fixture-id", reason="provider_handshake_read", error_type="ValueError")
    timing._output.queue.join()
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
    timing._output.queue.join()
    output = capsys.readouterr().err
    assert "private subprocess error" not in output
    assert len(output.splitlines()) == 2


def test_stalled_diagnostic_sink_cannot_delay_claims_or_tls_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    blocked, release = threading.Event(), threading.Event()

    class Sink:
        def write(self, text: str) -> int:
            if '"phase":"claim_received"' in text:
                blocked.set()
                if not release.wait(10):
                    raise TimeoutError("Fixture diagnostic sink stayed blocked")
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(timing, "sys", SimpleNamespace(stderr=Sink()))
    identifier = str(uuid.uuid4())
    with connected_channels(identifier) as (tunnel, channel), ThreadPoolExecutor(max_workers=1) as pool:
        def receive() -> None:
            with pytest.raises(TimeoutError):
                channel.recv(time.monotonic() + 2)
            assert channel.claim("handshake") == b'{"client_bytes":1}'
            channel.notify({"status": "running", "handshook": True, "authorized": False})
            assert channel.recv(time.monotonic() + 2) == b"request"
            channel.send(b"response")

        future = pool.submit(receive)
        try:
            status = tunnel.verify("handshake", b'{"client_bytes":1}', 2)
            assert status["handshook"] and not status["authorized"]
            assert blocked.wait(2)
            # A full diagnostic buffer must also leave capture traffic unblocked.
            for _ in range(2048):
                timing.transport("fixture", identifier)
            tunnel.send(b"request")
            assert tunnel.recv(timeout=2) == b"response"
            future.result(timeout=2)
        finally:
            release.set()
            timing._output.queue.join()
