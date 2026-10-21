from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pod import PodRuntime
from support import MemoryState
from test_recording import BODY, PRICING, SCOPE, invocation
from witness_client.calls import Invocation, canonical

from pod_gateway.budget import Budget
from pod_gateway.records import Call, CallRecords, RecordingConflict
from pod_gateway.recovery import recover_issued

START = "00000000-0000-4000-8000-000000000001"
PROOF = "c" * 64
USAGE = {"input_tokens": 10, "output_tokens": 2}


def capture(state: MemoryState) -> tuple[CallRecords, Budget, Invocation, Call]:
    records, budget, item = CallRecords(state), Budget(state, PRICING), invocation()
    call = records.reserve(SCOPE, item)
    assigned = records.assign(SCOPE, item, START, "pod-one")
    assert assigned.capture_id is not None
    assert records.claim(SCOPE, item, "pod-one", assigned.capture_id)
    budget.reserve(SCOPE, item, call.exchange_id, BODY)
    records.authorize(SCOPE, item, "pod-one", exchange_id=call.exchange_id)
    located = records.locate(SCOPE, call.exchange_id)
    assert located is not None
    return records, budget, item, located[1]


def evidence(budget: Budget, item: Invocation, call: Call) -> dict[str, bytes]:
    response = b"complete captured response"
    metadata = {
        "generation": item.generation.model_dump(),
        "generation_id": item.generation.id,
        "call_id": item.request.id,
        "request_key": item.request.model_dump(),
        "round": SCOPE.round,
        "hotkey": SCOPE.hotkey,
        "exchange_id": call.exchange_id,
        "pod_id": call.pod_id,
        "pod_start_id": call.start_id,
        "proof_key_sha256": PROOF,
        "pricing_sha256": budget.pricing_hash,
        "request_sha256": hashlib.sha256(BODY).hexdigest(),
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "provider_status": 200,
        "usage": USAGE,
    }
    return {
        "manifest.json": canonical(metadata),
        "request.json": BODY,
        "response.sse": response,
        "client.bin": b"retained request ciphertext",
        "server.bin": b"retained response ciphertext",
        "authorization-server.bin": b"authorization ciphertext",
        "claim.json": b"{}",
        "authorization.json": b"{}",
        "session.json": b"{}",
    }


def prefix(call: Call) -> str:
    return f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{call.exchange_id}/"


def runtime(state: MemoryState, records: CallRecords, budget: Budget, spool: Path) -> PodRuntime:
    return PodRuntime(
        evidence=state, records=records, budget=budget, scope=SCOPE, start_id=START, pod_id="pod-one",
        token="fixture", origin="https://witness.test", proof_hash=PROOF,
        round_end=int(time.time()) + 600, spool=spool,
    )


@pytest.mark.parametrize("settled", [False, True])
def test_complete_r2_capture_recovers_after_worker_loss(settled: bool) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    files = evidence(budget, item, call)
    for name, content in files.items():
        state.put(prefix(call) + name, content)
    if settled:
        budget.settle(SCOPE, item, USAGE)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: recover_issued(records, budget, state.read, SCOPE, item, call, PROOF), range(4))
        )
    assert all(result.phase == "issued" for result in results)
    located = records.locate(SCOPE, call.exchange_id)
    assert located is not None and located[1].phase == "issued"
    assert located[1].manifest_sha256 == hashlib.sha256(files["manifest.json"]).hexdigest()
    summary = budget.summary(SCOPE, item)
    assert summary["possible_dispatches"] == 1 and summary["pending_usage"] == 0
    assert state.get(prefix(call) + "response.sse") == files["response.sse"]


@pytest.mark.parametrize("missing", ["response.sse", "server.bin", "claim.json"])
def test_incomplete_r2_evidence_does_not_issue_a_result(missing: str) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    for name, content in evidence(budget, item, call).items():
        if name != missing:
            state.put(prefix(call) + name, content)
    recovered = recover_issued(records, budget, state.read, SCOPE, item, call, PROOF)
    assert recovered.phase == "capturing"
    assert budget.summary(SCOPE, item)["pending_usage"] == 1


@pytest.mark.parametrize("changed", ["pod_start_id", "pricing_sha256", "response_sha256"])
def test_recovery_rejects_mismatched_capture_evidence(changed: str) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    files = evidence(budget, item, call)
    metadata = json.loads(files["manifest.json"])
    metadata[changed] = "different"
    files["manifest.json"] = canonical(metadata)
    for name, content in files.items():
        state.put(prefix(call) + name, content)
    with pytest.raises(RecordingConflict, match="differs"):
        recover_issued(records, budget, state.read, SCOPE, item, call, PROOF)
    assert budget.summary(SCOPE, item)["pending_usage"] == 1


@pytest.mark.parametrize("completed", [False, True])
def test_restarted_worker_finishes_durable_capture(tmp_path: Path, completed: bool) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    directory = tmp_path / "spool" / call.exchange_id
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    for name, content in evidence(budget, item, call).items():
        state.put(prefix(call) + name, content)
        (directory / name).write_bytes(content)
    if completed:
        budget.settle(SCOPE, item, USAGE)
        records.issued(SCOPE, item, "pod-one", (directory / "manifest.json").read_bytes(), exchange_id=call.exchange_id)
    restarted = runtime(state, records, budget, directory.parent)
    restarted.executor.shutdown(wait=True)
    located = records.locate(SCOPE, call.exchange_id)
    assert located is not None and located[1].phase == "issued"
    assert budget.summary(SCOPE, item)["pending_usage"] == 0
    assert not directory.exists()


@pytest.mark.parametrize("manifest_saved", [False, True])
def test_restarted_worker_retains_pending_upload_until_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest_saved: bool
) -> None:
    blocked, release = threading.Event(), threading.Event()

    class PendingState(MemoryState):
        def put(self, key: str, data: bytes) -> None:
            blocked.set()
            if not release.wait(timeout=5):
                raise AssertionError("Upload was never released")
            super().put(key, data)

    state = PendingState()
    records, budget, item, call = capture(state)
    files = evidence(budget, item, call)
    MemoryState.put(state, prefix(call) + "session.json", files["session.json"])
    if manifest_saved:
        MemoryState.put(state, prefix(call) + "manifest.json", files["manifest.json"])
    directory = tmp_path / "spool" / call.exchange_id
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    for name, content in files.items():
        (directory / name).write_bytes(content)

    def finish(mode: str, path: Path, *, provider: str) -> dict[str, object]:
        assert mode == "finish" and path == directory
        return json.loads(files["manifest.json"])  # type: ignore[no-any-return]

    monkeypatch.setattr("pod.verify", finish)
    restarted = runtime(state, records, budget, directory.parent)
    try:
        assert blocked.wait(timeout=5)
        restarted.drain()
        assert directory.is_dir() and not restarted.tick()
        assert budget.summary(SCOPE, item)["pending_usage"] == 1
    finally:
        release.set()
        restarted.executor.shutdown(wait=True)
    located = records.locate(SCOPE, call.exchange_id)
    assert located is not None and located[1].phase == "issued"
    assert state.get(prefix(call) + "response.sse") == files["response.sse"]
    if manifest_saved:
        assert located[1].manifest_sha256 == hashlib.sha256(files["manifest.json"]).hexdigest()
    assert budget.summary(SCOPE, item)["pending_usage"] == 0
    assert not directory.exists()
    assert restarted.tick()


@pytest.mark.parametrize("issued", [False, True])
def test_completed_capture_releases_its_local_spool(tmp_path: Path, issued: bool) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    files = evidence(budget, item, call)
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    for name, content in files.items():
        (directory / name).write_bytes(content)
    state.put(prefix(call) + "session.json", files["session.json"])
    try:
        metadata = json.loads(files["manifest.json"]) if issued else None
        worker._finalize(item, call, metadata, "CaptureInterrupted")
        assert not directory.exists()
        located = records.locate(SCOPE, call.exchange_id)
        assert located is not None and located[1].phase == ("issued" if issued else "failed")
        assert state.get(prefix(call) + "server.bin") == files["server.bin"]
    finally:
        worker.executor.shutdown(wait=True)


def test_unclaimed_capture_spool_is_retained(tmp_path: Path) -> None:
    state = MemoryState()
    records, budget, item = CallRecords(state), Budget(state, PRICING), invocation()
    call = records.reserve(SCOPE, item)
    records.assign(SCOPE, item, START, "pod-one")
    directory = tmp_path / "spool" / call.exchange_id
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    (directory / "client.bin").write_bytes(b"partial")
    worker = runtime(state, records, budget, directory.parent)
    worker.executor.shutdown(wait=True)
    assert directory.is_dir() and (directory / "client.bin").read_bytes() == b"partial"
    located = records.locate(SCOPE, call.exchange_id)
    assert located is not None and located[1].phase == "assigned"


@pytest.mark.parametrize("already_issued", [False, True])
def test_failed_finalization_preserves_a_durable_response(tmp_path: Path, already_issued: bool) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    files = evidence(budget, item, call)
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    for name, content in files.items():
        (directory / name).write_bytes(content)
        state.put(prefix(call) + name, content)
    if already_issued:
        budget.settle(SCOPE, item, USAGE)
        records.issued(SCOPE, item, "pod-one", files["manifest.json"], exchange_id=call.exchange_id)
    try:
        worker._finalize(item, call, None, "ConnectionClosed")
        located = records.locate(SCOPE, call.exchange_id)
        assert located is not None and located[1].phase == "issued"
        assert located[1].manifest_sha256 == hashlib.sha256(files["manifest.json"]).hexdigest()
        assert budget.summary(SCOPE, item)["pending_usage"] == 0
        assert not directory.exists()
    finally:
        worker.executor.shutdown(wait=True)
