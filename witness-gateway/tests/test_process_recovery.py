"""SIGKILL a real PodRuntime process against local storage and provider fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from channel import WorkerChannel
from pod import PodRuntime
from support import MemoryState
from test_recording import BODY, PRICING, SCOPE, invocation
from test_recovery import PROOF, START, evidence, prefix, runtime

from pod_gateway.budget import Budget
from pod_gateway.records import CallRecords
from pod_gateway.recovery import recover_issued
from pod_gateway.state import Snapshot, StateConflict


class DiskState(MemoryState):
    """Committed SQLite objects outlive the worker; CAS matches the R2 fixture."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.database = directory / "objects.sqlite"
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS objects (key TEXT PRIMARY KEY, data BLOB, etag TEXT)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def load(self, key: str) -> Snapshot | None:
        with self.connect() as connection:
            row = connection.execute("SELECT etag, data FROM objects WHERE key = ?", (key,)).fetchone()
        return None if row is None else Snapshot(*row)

    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        etag = hashlib.sha256(data).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT etag FROM objects WHERE key = ?", (key,)).fetchone()
            if (None if row is None else row[0]) != expected:
                raise StateConflict("Concurrent fixture update")
            connection.execute("INSERT OR REPLACE INTO objects VALUES (?, ?, ?)", (key, data, etag))
        return Snapshot(etag, data)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO objects VALUES (?, ?, ?)", (key, data, hashlib.sha256(data).hexdigest())
            )


@contextmanager
def provider_fixture() -> Iterator[tuple[str, list[bytes]]]:
    requests: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            response = b"complete captured response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

        def log_message(self, *_: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


def child(directory: Path, mode: str, stage: str, origin: str) -> None:
    def pause(at: str) -> None:
        if mode == "capture" and at == stage:
            (directory / "reached").write_text(at)
            while True:
                signal.pause()

    class State(DiskState):
        def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
            result = super().save(key, data, expected)
            if key.startswith("pod-usage/"):
                charges = json.loads(data)["charges"].values()
                pause("settled" if any(c["state"] == "settled" for c in charges) else "reserved")
            if key.startswith("pod-calls/"):
                calls = [json.loads(data)["call"]]
                if any(c["phase"] == "issued" for c in calls):
                    pause("issued")
                elif any(c["authorized"] for c in calls):
                    pause("authorized")
            return result

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            super().put(key, data)
            if key.endswith("/response.sse"):
                pause("response_saved")
            if key.endswith("/manifest.json"):
                pause("manifest_saved")

    state = State(directory)
    records, budget, item = CallRecords(state), Budget(state, PRICING), invocation()
    call = records.reserve(SCOPE, item)

    def finish(_: str, spool: Path, **__: object) -> dict[str, object]:
        if (directory / "verification-unavailable").exists():
            (directory / "verification-attempted").touch()
            raise RuntimeError("Local verifier is temporarily unavailable")
        files = evidence(budget, item, call)
        if (spool / "server.bin").read_bytes() != files["server.bin"]:
            raise ValueError("Incomplete captured response")
        (spool / "response.sse").write_bytes(files["response.sse"])
        return json.loads(files["manifest.json"])  # type: ignore[no-any-return]

    patch = pytest.MonkeyPatch()
    patch.setattr("pod.verify", finish)

    def inspect_pending(worker: PodRuntime) -> None:
        if stage != "verifier_outage":
            return
        deadline = time.monotonic() + 5
        while not (directory / "verification-attempted").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with worker.lock:
            future = worker.active.get(call.exchange_id)
        if future is not None:
            try:
                future.result(timeout=0.2)
            except TimeoutError:
                pass
        worker.clock = lambda: worker.born + worker.idle_seconds + 1
        idle_drained = worker.tick()
        worker.drain()
        probe = directory / "pending-probe"
        probe.write_text(json.dumps({
            "active": len(worker.active), "idle_drained": idle_drained, "drained": worker.tick(),
        }))
        probe.replace(directory / "verification-pending")

    if mode == "recover":
        worker = runtime(state, records, budget, directory / "spool")
        inspect_pending(worker)
        worker.executor.shutdown(wait=True)
        return

    def relay(*args: Any, **kwargs: Any) -> dict[str, object]:
        spool = args[3]
        files = evidence(budget, item, call)
        for name in ("client.bin", "claim.json", "authorization.json", "request.json", "authorization-server.bin"):
            (spool / name).write_bytes(files[name])
        (spool / "server.bin").write_bytes(b"partial response")
        pause("before_authorization")
        kwargs["before_release"](BODY)
        with httpx.Client(trust_env=False) as client, client.stream("POST", origin, content=BODY) as response:
            chunks = response.iter_bytes(chunk_size=8)
            first = next(chunks)
            pause("active_response")
            assert first + b"".join(chunks) == files["response.sse"]
        (spool / "server.bin").write_bytes(files["server.bin"])
        pause("response_spooled")
        return finish("finish", spool)

    patch.setattr("pod.relay", relay)
    worker = runtime(state, records, budget, directory / "spool")
    worker.token = "a" * 64
    worker.accepting = True
    assert worker.start_capture(item, call, WorkerChannel(MagicMock()))
    inspect_pending(worker)
    worker.executor.shutdown(wait=True)


def launch(directory: Path, mode: str, stage: str = "", origin: str = "") -> subprocess.Popen[bytes]:
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(directory), mode, stage, origin],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


@pytest.mark.parametrize("stage", [
    "before_authorization", "reserved", "authorized", "active_response", "response_spooled",
    "response_saved", "manifest_saved", "settled", "issued",
])
@pytest.mark.parametrize("retain_spool", [True, False])
def test_sigkill_capture_recovers_without_repeating_a_possible_dispatch(
    tmp_path: Path, stage: str, retain_spool: bool,
) -> None:
    state = DiskState(tmp_path)
    records, budget, item = CallRecords(state), Budget(state, PRICING), invocation()
    call = records.reserve(SCOPE, item)
    call = records.assign(SCOPE, item, START, "pod-one")
    with provider_fixture() as (origin, requests):
        process = launch(tmp_path, "capture", stage, origin)
        try:
            deadline = time.monotonic() + 15
            while not (tmp_path / "reached").exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            assert (tmp_path / "reached").is_file(), process.communicate(timeout=1)[0].decode()
            process.kill()
            process.communicate(timeout=5)
            assert process.returncode == -signal.SIGKILL
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if not retain_spool:
            import shutil

            shutil.rmtree(tmp_path / "spool")
        restarted = launch(tmp_path, "recover")
        try:
            output = restarted.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            restarted.kill()
            output = restarted.communicate(timeout=5)[0]
            pytest.fail("Recovery did not finish within 10 seconds: " + output.decode())
        assert restarted.returncode == 0, output.decode()
        located = records.locate(SCOPE, call.exchange_id)
        assert located is not None
        recovered = recover_issued(records, budget, state.get, SCOPE, item, located[1], PROOF)
        complete = stage in ("response_spooled", "response_saved", "manifest_saved", "settled", "issued")
        durable = stage in ("manifest_saved", "settled", "issued")
        if complete and (retain_spool or durable):
            assert recovered.phase == "issued"
            assert state.get(prefix(call) + "response.sse") == evidence(budget, item, call)["response.sse"]
            assert budget.summary(SCOPE, item)["pending_usage"] == 0
        elif stage in ("before_authorization", "reserved"):
            assert recovered.phase == ("failed" if retain_spool else "capturing")
        else:
            assert recovered.phase == "capturing", "An uncertain paid attempt must not become retryable"
            assert budget.summary(SCOPE, item)["pending_usage"] == 1
            assert records.retry(SCOPE, item, call.exchange_id).exchange_id == call.exchange_id
        assert recovered.deadline == call.deadline
        assert len(requests) == int(stage in ("active_response", "response_spooled", "response_saved", "manifest_saved", "settled", "issued"))
        assert budget.summary(SCOPE, item)["possible_dispatches"] == int(stage != "before_authorization")
        if stage in ("response_saved", "manifest_saved", "settled", "issued"):
            assert state.get(prefix(call) + "response.sse") == evidence(budget, item, call)["response.sse"]
        summary = budget.summary(SCOPE, item)
        # A second restart repeats settlement/finalization without another provider request or charge.
        repeated = launch(tmp_path, "recover")
        try:
            output = repeated.communicate(timeout=10)[0]
        finally:
            if repeated.poll() is None:
                repeated.kill()
                repeated.communicate(timeout=5)
        assert repeated.returncode == 0, output.decode()
        assert len(requests) <= 1
        assert budget.summary(SCOPE, item) == summary


@pytest.mark.parametrize("restart", [False, True], ids=["live-capture", "startup-recovery"])
def test_retained_response_recovers_after_a_temporary_verifier_failure(tmp_path: Path, restart: bool) -> None:
    state = DiskState(tmp_path)
    records, budget, item = CallRecords(state), Budget(state, PRICING), invocation()
    call = records.reserve(SCOPE, item)
    records.assign(SCOPE, item, START, "pod-one")
    with provider_fixture() as (origin, requests):
        if restart:
            process = launch(tmp_path, "capture", "response_spooled", origin)
            try:
                deadline = time.monotonic() + 15
                while not (tmp_path / "reached").exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.02)
                assert (tmp_path / "reached").is_file(), process.communicate(timeout=1)[0].decode()
                process.kill()
                process.communicate(timeout=5)
                assert process.returncode == -signal.SIGKILL
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        unavailable = tmp_path / "verification-unavailable"
        unavailable.touch()
        pending = launch(tmp_path, "recover" if restart else "capture", "verifier_outage", origin)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "verification-pending").exists() and time.monotonic() < deadline and pending.poll() is None:
                time.sleep(0.02)
            assert (tmp_path / "verification-pending").is_file()
            assert json.loads((tmp_path / "verification-pending").read_bytes()) == {
                "active": 1, "idle_drained": False, "drained": False,
            }
            assert pending.poll() is None
            current = records.locate(SCOPE, call.exchange_id)
            assert current is not None and current[1].phase == "capturing" and current[1].authorized
            assert records.retry(SCOPE, item, call.exchange_id).exchange_id == call.exchange_id
            assert budget.summary(SCOPE, item)["pending_usage"] == 1
            assert len(requests) == budget.summary(SCOPE, item)["possible_dispatches"] == 1
            assert (tmp_path / "spool" / call.exchange_id / "server.bin").read_bytes() == evidence(
                budget, item, call,
            )["server.bin"]
            unavailable.unlink()
            output = pending.communicate(timeout=10)[0]
            assert pending.returncode == 0, output.decode()
        finally:
            if pending.poll() is None:
                pending.kill()
                pending.communicate(timeout=5)
        current = records.locate(SCOPE, call.exchange_id)
        assert current is not None and current[1].phase == "issued"
        assert current[1].deadline == call.deadline
        assert state.get(prefix(call) + "response.sse") == evidence(budget, item, call)["response.sse"]
        assert budget.summary(SCOPE, item)["pending_usage"] == 0
        assert budget.summary(SCOPE, item)["known_credits"] == "2.0"
        assert budget.summary(SCOPE, item)["possible_dispatches"] == len(requests) == 1
        assert not (tmp_path / "spool" / call.exchange_id).exists()


if __name__ == "__main__":
    child(Path(sys.argv[1]), *sys.argv[2:])
