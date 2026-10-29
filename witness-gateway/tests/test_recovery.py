from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from botocore.exceptions import EndpointConnectionError
from channel import WorkerChannel
from pod import PodRuntime
from support import MemoryState
from test_recording import BODY, PRICING, SCOPE, invocation
from test_state import FakeS3, s3_error
from witness_client.calls import RequestKey, canonical

from pod_gateway.budget import Budget
from pod_gateway.direct import Witness
from pod_gateway.records import Call, CallRecord, CallRecords, RecordingConflict
from pod_gateway.recovery import recover_issued
from pod_gateway.rounds import PromptImage, RoundInputs
from pod_gateway.state import R2StateStore, Snapshot, StateConflict, WriteUncertain

START = "00000000-0000-4000-8000-000000000001"
PROOF = "c" * 64
USAGE = {"input_tokens": 10, "output_tokens": 2}
RESPONSE = b"data: " + canonical({
    "type": "response.completed",
    "response": {"id": "fixture", "status": "completed", "output": [], "usage": USAGE},
}) + b"\n\n"


def capture(state: MemoryState) -> tuple[CallRecords, Budget, RequestKey, Call]:
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


def evidence(budget: Budget, item: RequestKey, call: Call) -> dict[str, bytes]:
    response = RESPONSE
    metadata = {
        "call_id": item.id,
        "request_key": item.model_dump(),
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


@pytest.mark.parametrize("provider_status", [404, 200])
@pytest.mark.parametrize("upload", ["server.bin", "manifest.json", "public"])
def test_failed_artifact_upload_does_not_block_an_ordinary_same_id_retry(
    tmp_path: Path, provider_status: int, upload: str,
) -> None:
    upload_failed = threading.Event()

    class ArtifactOutage(MemoryState):
        offline = False

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            blocked = key.startswith("evidence/v1/") if upload == "public" else key.endswith("/" + upload)
            if self.offline and blocked:
                upload_failed.set()
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            super().put(key, data, content_type)

    state = ArtifactOutage()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting = True
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    files = evidence(budget, item, call)
    response = (
        canonical({"error": {"message": "Fixture provider rejection"}})
        if provider_status == 404 else b"unusable provider response"
    )
    metadata = json.loads(files.pop("manifest.json"))
    metadata.update(provider_status=provider_status, usage=None, response_sha256=hashlib.sha256(response).hexdigest())
    files["response.sse"] = response
    for name, content in files.items():
        (directory / name).write_bytes(content)
        if name != "response.sse":
            state.put(prefix(call) + name, b"earlier provider handshake" if name == "server.bin" else content)
    state.offline = True
    future = worker.executor.submit(worker._finalize, item, call, metadata, "CaptureInterrupted")
    try:
        assert upload_failed.wait(timeout=5)
        failed = records.load(SCOPE, item.id)
        assert failed is not None and failed.call.phase == "failed"
        assert failed.call.failure == ("ProviderHTTPError" if provider_status == 404 else "InvalidProviderResponse")
        summary = budget.summary(SCOPE, item)
        assert summary["possible_dispatches"] == summary["pending_usage"] == 0
        assert directory.is_dir()
        request = MagicMock(spec=web.Request)
        request.read = AsyncMock(return_value=canonical({"request": item.model_dump()}))
        service = Witness(worker, lambda key: "https://witness.test/" + key)
        retry_response = asyncio.run(service.start(request))
        assert isinstance(retry_response.body, bytes)
        retry = Call.model_validate(json.loads(retry_response.body)["call"])
        assert retry.phase == "assigned" and retry.request == item
        assert retry.exchange_id != call.exchange_id
        budget.reserve(SCOPE, item, retry.exchange_id, BODY)
        state.offline = False
        future.result(timeout=10)
        assert not directory.exists()
        assert state.get(prefix(call) + "response.sse") == response
        saved = json.loads(state.get(prefix(call) + "failure.json") or b"{}")
        assert saved["error_type"] == failed.call.failure
        assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    finally:
        state.offline = False
        try:
            future.result(timeout=10)
        finally:
            worker.executor.shutdown(wait=True)


@pytest.mark.parametrize("provider_status", [404, 200])
def test_completed_failed_response_remains_readable_without_reusing_the_failure(
    tmp_path: Path, provider_status: int,
) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting = True
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    files = evidence(budget, item, call)
    response = (
        canonical({"error": {"message": "Fixture provider rejection"}})
        if provider_status == 404 else b"unusable provider response"
    )
    metadata = json.loads(files.pop("manifest.json"))
    metadata.update(provider_status=provider_status, usage=None, response_sha256=hashlib.sha256(response).hexdigest())
    files["response.sse"] = response
    for name, content in files.items():
        (directory / name).write_bytes(content)
    state.put(prefix(call) + "session.json", files["session.json"])
    try:
        worker._finalize(item, call, metadata, "CaptureInterrupted")
        located = records.locate(SCOPE, call.exchange_id)
        assert located is not None and located[1].phase == "failed"
        service = Witness(worker, lambda key: "https://witness.test/" + key)
        completion = service.completion(SCOPE, item, located[1])
        manifest = completion["manifest"]
        assert isinstance(manifest, str)
        saved = json.loads(manifest)
        assert saved["provider_status"] == provider_status
        assert saved["response_sha256"] == hashlib.sha256(response).hexdigest()
        assert budget.summary(SCOPE, item)["possible_dispatches"] == 0
        request = MagicMock(spec=web.Request)
        request.read = AsyncMock(return_value=canonical({"request": item.model_dump()}))
        retry_response = asyncio.run(service.start(request))
        assert isinstance(retry_response.body, bytes)
        retry = Call.model_validate(json.loads(retry_response.body)["call"])
        assert retry.phase == "assigned" and retry.exchange_id != call.exchange_id
    finally:
        worker.executor.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["partial", "http", "recover"])
def test_failed_diagnostic_write_does_not_block_an_ordinary_same_id_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    class DiagnosticOutage(MemoryState):
        offline = True

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            if self.offline and key.endswith("/failure.json"):
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            super().put(key, data, content_type)

    state = DiagnosticOutage()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting = True
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    files = evidence(budget, item, call)
    metadata = json.loads(files["manifest.json"])
    response = canonical({"error": {"message": "Fixture provider rejection"}})
    metadata.update(provider_status=404, usage=None, response_sha256=hashlib.sha256(response).hexdigest())
    files["manifest.json"], files["response.sse"] = canonical(metadata), response
    if operation == "partial":
        files.pop("manifest.json")
        files.pop("response.sse")
    for name, content in files.items():
        if operation != "http" or name != "manifest.json":
            state.put(prefix(call) + name, content)
        (directory / name).write_bytes(content)
    monkeypatch.setattr(worker, "_retry_upload", lambda operation: operation())
    try:
        with pytest.raises(EndpointConnectionError):
            if operation == "recover":
                recover_issued(records, budget, state.read, SCOPE, item, call, PROOF, worker.save_failure)
            else:
                worker._finalize(item, call, metadata if operation == "http" else None, "CaptureInterrupted")
        failed = records.load(SCOPE, item.id)
        assert failed is not None and failed.call.phase == "failed"
        summary = budget.summary(SCOPE, item)
        assert summary["possible_dispatches"] == summary["pending_usage"] == 0
        assert directory.is_dir()
        request = MagicMock(spec=web.Request)
        request.read = AsyncMock(return_value=canonical({"request": item.model_dump()}))
        service = Witness(worker, lambda key: "https://witness.test/" + key)
        retry_response = asyncio.run(service.start(request))
        assert isinstance(retry_response.body, bytes)
        retry = json.loads(retry_response.body)["call"]
        assert retry["phase"] == "assigned" and retry["exchange_id"] != call.exchange_id
        assert retry["request"] == item.model_dump()
        budget.reserve(SCOPE, item, retry["exchange_id"], BODY)
        assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
        state.offline = False
        if operation == "recover":
            worker._recover(call.exchange_id)
        else:
            worker._finalize(item, call, metadata if operation == "http" else None, "CaptureInterrupted")
        assert not directory.exists()
        saved_failure = json.loads(state.get(prefix(call) + "failure.json") or b"{}")
        assert saved_failure["error_type"] == ("CaptureInterrupted" if operation == "partial" else "ProviderHTTPError")
        assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    finally:
        worker.executor.shutdown(wait=True)


@pytest.mark.parametrize("replaced", [False, True])
def test_expired_capture_saves_partial_evidence_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool,
) -> None:
    class EvidenceOutage(MemoryState):
        offline = False

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            if self.offline:
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            super().put(key, data, content_type)

    state = EvidenceOutage()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting = True
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    files = evidence(budget, item, call)
    files.pop("manifest.json")
    files.pop("response.sse")
    files["server.bin"] = b"provider handshake ciphertext"
    files["authorization-server.bin"] = files["server.bin"]
    files["authorization.json"] = canonical({"sessionId": item.session_id})
    for name, content in files.items():
        state.put(prefix(call) + name, content)
        (directory / name).write_bytes(content)
    partial = files["server.bin"] + b" plus partial provider response ciphertext"
    (directory / "server.bin").write_bytes(partial)

    def expire(record: CallRecord) -> None:
        record.call.deadline = int(time.time()) - 1

    records._change(SCOPE, item, expire)
    expired = records.load(SCOPE, item.id)
    assert expired is not None
    service = Witness(worker, lambda key: "https://witness.test/" + key)
    try:
        snapshot = asyncio.run(service._snapshot(SCOPE, item, expired.call))
        assert Call.model_validate(snapshot["call"]).phase == "failed"
        if replaced:
            retry = records.assign(SCOPE, item, START, "pod-one", expected_exchange_id=call.exchange_id)
            budget.reserve(SCOPE, item, retry.exchange_id, BODY)
        monkeypatch.setattr(worker, "_retry_upload", lambda operation: operation())
        state.offline = True
        with pytest.raises(EndpointConnectionError):
            worker._recover(call.exchange_id)
        assert directory.is_dir() and (directory / "server.bin").read_bytes() == partial
        state.offline = False
        worker._recover(call.exchange_id)
        assert not directory.exists()
        assert state.get(prefix(call) + "server.bin") == partial
        assert json.loads(state.get(prefix(call) + "failure.json") or b"{}")["error_type"] == "CaptureExpired"
        if replaced:
            assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    finally:
        worker.executor.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["issued", "failed", "recover"])
@pytest.mark.parametrize("status", [None, "issued"])
def test_replaced_failed_capture_cleans_up_without_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, status: str | None,
) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    worker = runtime(state, records, budget, tmp_path / "spool")
    directory = worker.spool / call.exchange_id
    directory.mkdir(mode=0o700)
    files = evidence(budget, item, call)
    response = canonical({"error": {"message": "Fixture provider rejection"}})
    metadata = json.loads(files["manifest.json"])
    metadata.update(provider_status=404, usage=None, response_sha256=hashlib.sha256(response).hexdigest())
    files["manifest.json"], files["response.sse"] = canonical(metadata), response
    for name, content in files.items():
        state.put(prefix(call) + name, content)
        (directory / name).write_bytes(content)
    monkeypatch.setattr(worker, "_retry_upload", lambda operation: operation())
    try:
        assert recover_issued(
            records, budget, state.read, SCOPE, item, call, PROOF, worker.save_failure,
        ).phase == "failed"
        retry = records.assign(SCOPE, item, START, "pod-one", expected_exchange_id=call.exchange_id)
        budget.reserve(SCOPE, item, retry.exchange_id, BODY)
        budget.settle(SCOPE, item, USAGE, exchange_id=retry.exchange_id)
        if status is None:
            state.delete(prefix(call) + "failure.json")
        else:
            state.put(prefix(call) + "status.json", canonical({"status": status}))
        if operation == "recover":
            worker._recover(call.exchange_id)
        else:
            worker._finalize(item, call, metadata if operation == "issued" else None, "CaptureInterrupted")
        assert not directory.exists()
        current = records.load(SCOPE, item.id)
        assert current is not None and current.call.exchange_id == retry.exchange_id
        assert budget.summary(SCOPE, item)["known_credits"] == "2.0"
        if status is not None:
            assert json.loads(state.get(prefix(call) + "failure.json") or b"{}")["error_type"] == "ProviderHTTPError"
    finally:
        worker.executor.shutdown(wait=True)


def test_permanently_drained_worker_reconnects_until_retirement_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MemoryState()
    worker = runtime(state, CallRecords(state), Budget(state, PRICING), tmp_path / "spool")
    sockets = [MagicMock(), MagicMock()]
    drain = json.dumps({"type": "drain", "id": "selection"})
    sockets[0].recv.side_effect = [drain, OSError("Fixture gateway restarted")]
    sockets[1].recv.side_effect = [drain, json.dumps({"type": "retired"})]
    connections: list[MagicMock] = []

    def connect(*args: object, **kwargs: object) -> MagicMock:
        socket = sockets[len(connections)]
        socket.__enter__.return_value = socket
        connections.append(socket)
        return socket

    monkeypatch.setattr("pod.connect", connect)
    monkeypatch.setattr("pod.time.sleep", lambda _: None)
    worker.run()
    assert len(connections) == 2
    for socket in sockets:
        assert {"type": "drained"} in [json.loads(call.args[0]) for call in socket.send.call_args_list]
    assert not worker.selected()


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


def test_restarted_failed_capture_saves_failure_with_its_storage_prefix(tmp_path: Path) -> None:
    state = MemoryState()
    records, budget, item, call = capture(state)
    storage_prefix = "isolated/"
    files = evidence(budget, item, call)
    response = canonical({"error": {"message": "Fixture provider rejection"}})
    metadata = json.loads(files["manifest.json"])
    metadata.update(provider_status=404, usage=None, response_sha256=hashlib.sha256(response).hexdigest())
    files["manifest.json"], files["response.sse"] = canonical(metadata), response
    for name, content in files.items():
        state.put(storage_prefix + prefix(call) + name, content)
    status_key = storage_prefix + prefix(call) + "failure.json"
    state.put(storage_prefix + prefix(call) + "status.json", canonical({"status": "issued"}))
    directory = tmp_path / "spool" / call.exchange_id
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    inputs = RoundInputs(
        repository="404-repo/404-active-competition", branch="main", revision="a" * 40,
        round=SCOPE.round, number=0, seed=42, generation_deadline_block=None, storage_prefix=storage_prefix,
        prompts=(PromptImage(id="chair", source_url="https://images.test/chair.png", sha256="d" * 64,
                             image_key=storage_prefix + "competition-inputs/v1/" + "d" * 64),),
    )
    restarted = PodRuntime(
        evidence=state, records=records, budget=budget, scope=SCOPE, start_id=START, pod_id="pod-one",
        token="fixture", origin="https://witness.test", proof_hash=PROOF,
        round_end=None, spool=directory.parent, round_inputs=inputs,
    )
    restarted.executor.shutdown(wait=True)
    status = state.get(status_key)
    assert status is not None
    assert json.loads(status)["error_type"] == "ProviderHTTPError"
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 0


@pytest.mark.parametrize("manifest_saved", [False, True])
def test_restarted_worker_retains_pending_upload_until_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest_saved: bool
) -> None:
    blocked, release = threading.Event(), threading.Event()

    class PendingState(MemoryState):
        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
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


@pytest.mark.parametrize("committed", [False, True])
def test_restart_recovers_interrupted_claim_without_consuming_a_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool,
) -> None:
    state, item = MemoryState(), invocation()
    records, budget = CallRecords(state), Budget(state, PRICING)
    original = records.reserve(SCOPE, item)
    assigned = records.assign(SCOPE, item, START, "pod-one")
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting = True
    claim = records.claim

    def interrupted(*_args: object) -> bool:
        if committed:
            claim(SCOPE, item, "pod-one", assigned.capture_id or "")
        raise SystemExit("Fixture stopped before claim acknowledgement")

    monkeypatch.setattr(records, "claim", interrupted)
    try:
        with pytest.raises(SystemExit):
            worker.start_capture(item, assigned, WorkerChannel(MagicMock()))
    finally:
        worker.executor.shutdown(wait=True)
    monkeypatch.setattr(records, "claim", claim)
    restarted = runtime(state, records, budget, worker.spool)
    restarted.executor.shutdown(wait=True)
    located = records.locate(SCOPE, original.exchange_id)
    assert located is not None and located[1].phase == "failed" and located[1].not_dispatched
    retried = records.retry(SCOPE, item, original.exchange_id)
    assert retried.phase == "reserved" and retried.exchange_id != original.exchange_id
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 0


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


@pytest.mark.parametrize("not_dispatched", [False, True])
def test_unreserved_retry_failure_does_not_block_the_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, not_dispatched: bool,
) -> None:
    state = MemoryState()
    records, budget, item, first = capture(state)
    budget.discard(SCOPE, item, first.exchange_id)
    records.failed(SCOPE, item, "pod-one", "InvalidProviderResponse", exchange_id=first.exchange_id)
    spending = budget.summary(SCOPE, item)
    second = records.assign(SCOPE, item, START, "pod-one", expected_exchange_id=first.exchange_id)
    assert records.claim(SCOPE, item, "pod-one", second.capture_id or "")
    worker = runtime(state, records, budget, tmp_path / "spool")
    (worker.spool / second.exchange_id).mkdir(mode=0o700)
    monkeypatch.setattr(worker, "_retry_upload", lambda operation: operation())
    try:
        worker._finalize(item, second, None, "SetupTimeout", not_dispatched=not_dispatched)
    finally:
        worker.executor.shutdown(wait=True)
    failed = records.locate(SCOPE, second.exchange_id)
    assert failed is not None and failed[1].phase == "failed"
    assert budget.summary(SCOPE, item) == spending
    assert json.loads(state.get(prefix(second) + "failure.json") or b"{}")["error_type"] == "SetupTimeout"

    third = records.assign(SCOPE, item, START, "pod-one", expected_exchange_id=second.exchange_id)
    assert third.request == item and third.exchange_id != second.exchange_id
    assert records.claim(SCOPE, item, "pod-one", third.capture_id or "")
    budget.reserve(SCOPE, item, third.exchange_id, BODY)
    records.authorize(SCOPE, item, "pod-one", exchange_id=third.exchange_id)
    budget.settle(SCOPE, item, USAGE, exchange_id=third.exchange_id)
    records.issued(SCOPE, item, "pod-one", evidence(budget, item, third)["manifest.json"], exchange_id=third.exchange_id)
    saved = records.load(SCOPE, item.id)
    assert saved is not None and saved.call.phase == "issued"
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    assert budget.summary(SCOPE, item)["pending_usage"] == 0


@pytest.mark.parametrize("stage", [
    "before_budget", "budget", "budget_read", "budget_conflicts", "authorization", "after_authorization",
])
@pytest.mark.parametrize("committed", [False, True])
def test_failed_capture_releases_its_charge_before_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, committed: bool,
) -> None:
    class UncertainState(MemoryState):
        pending_key: str | None = None
        read_failure = False
        conflicts = 0

        def load(self, key: str) -> Snapshot | None:
            if key.startswith("pod-usage/") and self.read_failure:
                self.read_failure = False
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            return super().load(key)

        def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
            if key.startswith("pod-usage/") and self.conflicts:
                self.conflicts -= 1
                raise StateConflict("Fixture conditional write did not commit")
            if key == self.pending_key:
                self.pending_key = None
                if committed:
                    super().save(key, data, expected)
                raise WriteUncertain("Fixture write acknowledgement lost")
            return super().save(key, data, expected)

    state, item = UncertainState(), invocation()
    records, budget = CallRecords(state), Budget(state, PRICING)
    original = records.reserve(SCOPE, item)
    call = records.assign(SCOPE, item, START, "pod-one")
    assert records.claim(SCOPE, item, "pod-one", call.capture_id or "")
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting = True
    worker.token = "ab" * 32

    def relay(*args: object, before_release: Callable[[bytes], None], **kwargs: object) -> None:
        if stage == "before_budget":
            raise TimeoutError("Fixture failed before authorization")
        if stage == "budget":
            state.pending_key = budget.key(SCOPE, item)
        elif stage == "budget_read":
            state.read_failure = True
        elif stage == "budget_conflicts":
            state.conflicts = 16
        elif stage == "authorization":
            state.pending_key = records.key(SCOPE, item.id)
        directory = args[3]
        assert isinstance(directory, Path)
        (directory / "authorization.json").write_bytes(b"{}")
        before_release(BODY)
        raise TimeoutError("Fixture failed after authorization")

    monkeypatch.setattr("pod.connect", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("pod.relay", relay)
    try:
        worker._capture(item, call, MagicMock())
    finally:
        worker.executor.shutdown(wait=True)
    located = records.locate(SCOPE, original.exchange_id)
    assert located is not None and located[1].phase == "failed"
    retried = records.retry(SCOPE, item, original.exchange_id)
    assert retried.exchange_id != original.exchange_id and retried.phase == "reserved"
    summary = budget.summary(SCOPE, item)
    assert summary["possible_dispatches"] == summary["pending_usage"] == 0
    budget.reserve(SCOPE, item, retried.exchange_id, BODY)
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1


@pytest.mark.parametrize("mode", [
    "status-400", "status-403", "status-429", "status-408", "request-timeout", "status-503",
    "timeout-before", "timeout-after",
])
def test_capture_retries_rejected_budget_writes_but_not_uncertain_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    class PromptS3(FakeS3):
        def get_object(self, **kwargs: object) -> dict[str, object]:
            return super().get_object(Bucket="private", Key="journal")

        def put_object(self, **kwargs: object) -> dict[str, object]:
            if self.mode == "request-timeout":
                raise s3_error("RequestTimeout", 400)
            return super().put_object(**kwargs)

    state, item, s3 = MemoryState(), invocation(), PromptS3()
    records, budget = CallRecords(state), Budget(R2StateStore(s3, "private"), PRICING)
    original = records.reserve(SCOPE, item)
    call = records.assign(SCOPE, item, START, "pod-one")
    assert records.claim(SCOPE, item, "pod-one", call.capture_id or "")
    worker = runtime(state, records, budget, tmp_path / "spool")
    worker.accepting, worker.token = True, "ab" * 32
    released = 0
    s3.mode = mode

    def relay(*args: object, before_release: Callable[[bytes], None], **kwargs: object) -> None:
        nonlocal released
        directory = args[3]
        assert isinstance(directory, Path)
        (directory / "authorization.json").write_bytes(b"{}")
        try:
            before_release(BODY)
        finally:
            s3.mode = "ok"
        released += 1
        raise TimeoutError("Fixture failed after provider release")

    monkeypatch.setattr("pod.connect", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("pod.relay", relay)
    try:
        worker._capture(item, call, MagicMock())
    finally:
        worker.executor.shutdown(wait=True)

    summary = budget.summary(SCOPE, item)
    reserved = int(mode == "timeout-after")
    assert released == reserved
    assert summary["possible_dispatches"] == summary["pending_usage"] == 0
    s3.mode = "ok"
    retried = records.retry(SCOPE, item, original.exchange_id)
    assert retried.exchange_id != original.exchange_id and retried.phase == "reserved"
    budget.reserve(SCOPE, item, retried.exchange_id, BODY)
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
