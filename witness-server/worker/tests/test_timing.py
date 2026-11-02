"""Worker diagnostics contain no transcript or subprocess error material."""

import json
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from types import SimpleNamespace

import pytest
from conftest import connected_channels

import timing


@pytest.fixture
def stalls(monkeypatch: pytest.MonkeyPatch) -> Iterator[Queue[dict[str, object]]]:
    records: Queue[dict[str, object]] = Queue()

    class Output:
        def write(self, value: object) -> None:
            assert isinstance(value, dict)
            records.put(value)

    watchdog = timing._StallWatchdog(threshold=0.1, interval=0.05, poll=0.01)
    monkeypatch.setattr(timing, "_watchdog", watchdog)
    monkeypatch.setattr(timing, "_stall_output", Output())
    monkeypatch.setenv("WITNESS_TIMINGS", "1")
    try:
        yield records
    finally:
        watchdog.close()


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


def test_stall_samples_the_blocked_worker_without_request_data(
    stalls: Queue[dict[str, object]],
) -> None:
    release = threading.Event()
    identifier = str(uuid.uuid4())

    def persist_transcript() -> tuple[int, int]:
        request = {"Authorization": "Bearer fixture-secret-do-not-log", "prompt": "fixture-private-prompt"}
        with timing.measure("pod_capture"):
            timing.identify_exchange(identifier)
            with timing.measure("claim_spool_persistence"):
                assert release.wait(3)
        assert request["Authorization"].startswith("Bearer")
        return threading.get_ident(), threading.get_native_id()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(persist_transcript)
        try:
            samples = [stalls.get(timeout=2) for _ in range(3)]
            with pytest.raises(Empty):
                stalls.get(timeout=0.15)
        finally:
            release.set()
        thread_id, native_id = future.result(timeout=2)
    for sample in samples:
        assert sample["event"] == "witness_worker_stall"
        assert sample["exchange_id"] == identifier
        assert sample["phase"] == "claim_spool_persistence"
        assert (sample["thread_id"], sample["native_thread_id"]) == (thread_id, native_id)
        assert thread_id != threading.get_ident()
        stack = sample["stack"]
        assert isinstance(stack, list)
        assert any(frame["function"] == "persist_transcript" for frame in stack)
        assert all(frame["line"] > 0 and not frame["file"].startswith("/") for frame in stack)
        output = json.dumps(sample)
        assert "fixture-secret-do-not-log" not in output
        assert "fixture-private-prompt" not in output
    with pytest.raises(Empty):
        stalls.get(timeout=0.15)


def test_concurrent_stalls_keep_their_own_exchange_and_phase(
    stalls: Queue[dict[str, object]],
) -> None:
    release = threading.Event()
    phases = {str(uuid.uuid4()): "claim_spool_persistence", str(uuid.uuid4()): "verify_handshake"}

    def blocked(identifier: str, phase: str) -> int:
        with timing.measure("pod_capture"):
            timing.identify_exchange(identifier)
            with timing.measure(phase):
                assert release.wait(3)
        return threading.get_ident()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {identifier: pool.submit(blocked, identifier, phase) for identifier, phase in phases.items()}
        samples: dict[str, dict[str, object]] = {}
        try:
            while len(samples) < 2:
                sample = stalls.get(timeout=2)
                samples[str(sample["exchange_id"])] = sample
        finally:
            release.set()
        assert len({future.result(timeout=2) for future in futures.values()}) == 2
        for identifier, sample in samples.items():
            assert sample["phase"] == phases[identifier]
            assert sample["thread_id"] == futures[identifier].result()


@pytest.mark.parametrize("phase", ["provider_response_capture", "evidence_retry_wait"])
def test_polling_and_intentional_backoff_do_not_report_stalls(
    stalls: Queue[dict[str, object]], phase: str,
) -> None:
    with timing.measure(phase):
        timing.identify_exchange(str(uuid.uuid4()))
        for _ in range(30):
            if phase == "provider_response_capture":
                timing.progress()
            time.sleep(0.01)
    assert stalls.empty()


def test_stall_sampling_is_disabled_without_timings(
    monkeypatch: pytest.MonkeyPatch, stalls: Queue[dict[str, object]],
) -> None:
    monkeypatch.delenv("WITNESS_TIMINGS")
    with timing.measure("claim_spool_persistence"):
        timing.identify_exchange(str(uuid.uuid4()))
        time.sleep(0.2)
    assert stalls.empty()


def test_stack_report_survives_a_full_blocked_routine_log_queue(
    monkeypatch: pytest.MonkeyPatch, stalls: Queue[dict[str, object]],
) -> None:
    sink_blocked, stall_blocked = threading.Event(), threading.Event()
    sink_release, worker_release = threading.Event(), threading.Event()
    written: list[str] = []

    class Sink:
        def write(self, text: str) -> int:
            if '"event":"witness_worker_stall"' in text:
                stall_blocked.set()
            sink_blocked.set()
            assert sink_release.wait(3)
            written.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    routine = timing._DiagnosticOutput(capacity=1)
    critical = timing._DiagnosticOutput(capacity=4, name="fixture-stall-output")
    monkeypatch.setattr(timing, "sys", SimpleNamespace(stderr=Sink(), _current_frames=sys._current_frames))
    monkeypatch.setattr(timing, "_output", routine)
    monkeypatch.setattr(timing, "_stall_output", critical)

    def persist_transcript() -> None:
        with timing.measure("claim_spool_persistence"):
            timing.identify_exchange(str(uuid.uuid4()))
            assert worker_release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(persist_transcript)
        try:
            timing.transport("fixture", "fixture-id")
            assert sink_blocked.wait(2)
            for _ in range(10):
                timing.transport("fixture", "fixture-id")
            assert stall_blocked.wait(2)
            worker_release.set()
            future.result(timeout=2)
        finally:
            worker_release.set()
            sink_release.set()
            routine.queue.join()
            critical.queue.join()
    reports = [json.loads(line) for line in "".join(written).splitlines()]
    assert any(
        report["event"] == "witness_worker_stall"
        and any(frame["function"] == "persist_transcript" for frame in report["stack"])
        for report in reports
    )
