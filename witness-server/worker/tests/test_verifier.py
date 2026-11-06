"""Retained evidence rejection is distinct from a failed verifier process."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from witness_pod.diagnostics import FailureObservation, verifier_failure
from witness_pod.worker import verify


@pytest.mark.parametrize(
    "returncode,output,error",
    [(2, b"", ValueError), (1, b"", RuntimeError), (-9, b"", RuntimeError), (0, b"{", RuntimeError)],
)
def test_finish_distinguishes_rejection_from_verifier_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, output: bytes, error: type[Exception],
) -> None:
    monkeypatch.setattr(
        "witness_pod.worker.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], returncode, output, b"private verifier error")),
    )
    observation = FailureObservation()
    with pytest.raises(error):
        verify("finish", tmp_path, provider="api", observe_failure=observation.observe)
    diagnostic = observation.failure
    assert diagnostic is not None and diagnostic.verifier is not None
    assert diagnostic.verifier.mode == "finish" and diagnostic.verifier.returncode == returncode
    assert diagnostic.reason == {
        2: "verification_rejected", 1: "verifier_process_failed", -9: "verifier_process_failed",
        0: "verifier_invalid_result",
    }[returncode]
    assert diagnostic.stack[0].function == "verify"
    assert {frame.file for frame in diagnostic.stack} == {"witness_pod/worker.py"}
    assert "private verifier error" not in diagnostic.model_dump_json()


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("verifier", 180), OSError("verifier unavailable")])
def test_finish_preserves_verifier_infrastructure_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setattr("witness_pod.worker.subprocess.run", MagicMock(side_effect=error))
    observation = FailureObservation()
    with pytest.raises(type(error)):
        verify("finish", tmp_path, provider="api", observe_failure=observation.observe)
    diagnostic = observation.failure
    assert diagnostic is not None and diagnostic.verifier is not None
    assert diagnostic.verifier.mode == "finish" and diagnostic.verifier.returncode is None
    assert diagnostic.reason == ("verifier_timeout" if isinstance(error, subprocess.TimeoutExpired)
                                 else "verifier_unavailable")


def test_verifier_observation_keeps_the_originating_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = MagicMock(return_value=subprocess.CompletedProcess([], 2, b"", b"private rejection"))
    monkeypatch.setattr("witness_pod.worker.subprocess.run", process)
    observation = FailureObservation()
    with pytest.raises(ValueError):
        verify("finish", tmp_path, provider="api", observe_failure=observation.observe)
    original = observation.failure
    process.return_value = subprocess.CompletedProcess([], -9, b"", b"private crash")
    with pytest.raises(RuntimeError):
        verify("finish", tmp_path, provider="api", observe_failure=observation.observe)
    assert observation.failure is original


def test_verifier_failure_keeps_safe_frames_without_stderr_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-proof-or-provider-value"
    stderr = json.dumps({
        "event": "witness_verifier_failure", "mode": "handshake", "error_type": "TypeError",
        "message": private,
        "stack": [
            {"file": "verifier/verify.mjs", "function": "authorizeHandshake", "line": 20, "column": 3,
             "locals": private},
        ],
    }).encode() + b"\n" + private.encode()
    monkeypatch.setattr("witness_pod.worker.subprocess.run", MagicMock(
        return_value=subprocess.CompletedProcess([], 1, b"", stderr),
    ))
    observation = FailureObservation()
    with pytest.raises(ValueError):
        verify("handshake", tmp_path, provider="api", observe_failure=observation.observe)
    assert observation.failure is not None and observation.failure.verifier is not None
    diagnostic = observation.failure.verifier
    assert diagnostic.model_dump() == {
        "mode": "handshake", "returncode": 1, "error_type": "TypeError",
        "stack": ({"file": "verifier/verify.mjs", "function": "authorizeHandshake", "line": 20, "column": 3},),
    }
    assert private not in observation.failure.model_dump_json()


@pytest.mark.parametrize("replacement", [
    {"file": "private/request.json"}, {"function": "private value"}, {"line": True}, {"column": 0},
])
def test_verifier_ignores_malformed_failure_events(replacement: dict[str, object]) -> None:
    frame = {"file": "verifier/verify.mjs", "function": None, "line": 1, "column": 2, **replacement}
    stderr = json.dumps({
        "event": "witness_verifier_failure", "mode": "finish", "error_type": "TypeError", "stack": [frame],
    }).encode()
    failure = verifier_failure(stderr, "finish", 2)
    assert failure.error_type is None and failure.stack == ()


@pytest.mark.parametrize("authorization", [None, b"{", b"{}"])
def test_finish_rejects_missing_or_invalid_evidence(tmp_path: Path, authorization: bytes | None) -> None:
    if authorization is not None:
        (tmp_path / "authorization.json").write_bytes(authorization)
    (tmp_path / "server.bin").write_bytes(b"")
    observation = FailureObservation()
    with pytest.raises(ValueError, match="verification rejected"):
        verify("finish", tmp_path, provider="api", observe_failure=observation.observe)
    assert not (tmp_path / "response.sse").exists()
    assert observation.failure is not None and observation.failure.verifier is not None
    diagnostic = observation.failure.verifier
    assert diagnostic.stack
    if authorization == b"{":
        assert diagnostic.error_type == "SyntaxError"
