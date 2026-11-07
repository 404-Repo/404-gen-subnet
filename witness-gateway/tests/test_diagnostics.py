from __future__ import annotations

import io
import json
import threading

import pytest

from pod_gateway.diagnostics import EventOutput


def test_blocked_stdout_bounds_gateway_events_and_reports_dropped_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()

    class Blocked(io.StringIO):
        def write(self, text: str) -> int:
            entered.set()
            assert release.wait(3)
            return super().write(text)

    stream = Blocked()
    output = EventOutput(capacity=2)
    with monkeypatch.context() as patch:
        patch.setattr("pod_gateway.diagnostics.sys.stdout", stream)
        try:
            output.write("first")
            assert entered.wait(1)
            for _ in range(20):
                output.write("gateway_request", status=429, error="request_rate_limited")
            assert output.queue.qsize() == 2
        finally:
            release.set()
            output.queue.join()
        output.write("recovered")
        output.queue.join()
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert rows[0]["event"] == "first" and rows[-1]["event"] == "recovered"
    assert rows[-1]["dropped_logs"] == 18
