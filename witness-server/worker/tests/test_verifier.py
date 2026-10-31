"""Retained evidence rejection is distinct from a failed verifier process."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from worker import verify


@pytest.mark.parametrize(
    "returncode,output,error",
    [(2, b"", ValueError), (1, b"", RuntimeError), (-9, b"", RuntimeError), (0, b"{", RuntimeError)],
)
def test_finish_distinguishes_rejection_from_verifier_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, output: bytes, error: type[Exception],
) -> None:
    monkeypatch.setattr(
        "worker.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], returncode, output, b"private verifier error")),
    )
    with pytest.raises(error):
        verify("finish", tmp_path, provider="api")
    saved = (tmp_path / "failure.json").read_bytes()
    diagnostic = json.loads(saved)["diagnostic"]
    assert diagnostic["verifier"] == {"mode": "finish", "returncode": returncode}
    assert diagnostic["reason"] == {
        2: "verification_rejected", 1: "verifier_process_failed", -9: "verifier_process_failed",
        0: "verifier_invalid_result",
    }[returncode]
    assert diagnostic["stack"][-1]["function"] == "verify"
    assert b"private verifier error" not in saved


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("verifier", 180), OSError("verifier unavailable")])
def test_finish_preserves_verifier_infrastructure_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setattr("worker.subprocess.run", MagicMock(side_effect=error))
    with pytest.raises(type(error)):
        verify("finish", tmp_path, provider="api")
    diagnostic = json.loads((tmp_path / "failure.json").read_bytes())["diagnostic"]
    assert diagnostic["verifier"] == {"mode": "finish", "returncode": None}
    assert diagnostic["reason"] == ("verifier_timeout" if isinstance(error, subprocess.TimeoutExpired)
                                    else "verifier_unavailable")


def test_verifier_recovery_does_not_replace_the_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = MagicMock(return_value=subprocess.CompletedProcess([], 2, b"", b"private rejection"))
    monkeypatch.setattr("worker.subprocess.run", process)
    with pytest.raises(ValueError):
        verify("finish", tmp_path, provider="api")
    original = (tmp_path / "failure.json").read_bytes()
    process.return_value = subprocess.CompletedProcess([], -9, b"", b"private crash")
    with pytest.raises(RuntimeError):
        verify("finish", tmp_path, provider="api")
    assert (tmp_path / "failure.json").read_bytes() == original


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
            {"file": private, "function": private, "line": 1, "column": 1},
        ],
    }).encode() + b"\n" + private.encode()
    monkeypatch.setattr("worker.subprocess.run", MagicMock(
        return_value=subprocess.CompletedProcess([], 1, b"", stderr),
    ))
    with pytest.raises(ValueError):
        verify("handshake", tmp_path, provider="api")
    saved = (tmp_path / "failure.json").read_bytes()
    diagnostic = json.loads(saved)["diagnostic"]["verifier"]
    assert diagnostic == {
        "mode": "handshake", "returncode": 1, "error_type": "TypeError",
        "stack": [{"file": "verifier/verify.mjs", "function": "authorizeHandshake", "line": 20, "column": 3}],
    }
    assert private.encode() not in saved


@pytest.mark.parametrize("authorization", [None, b"{", b"{}"])
def test_finish_rejects_missing_or_invalid_evidence(tmp_path: Path, authorization: bytes | None) -> None:
    if authorization is not None:
        (tmp_path / "authorization.json").write_bytes(authorization)
    (tmp_path / "server.bin").write_bytes(b"")
    with pytest.raises(ValueError, match="verification rejected"):
        verify("finish", tmp_path, provider="api")
    assert not (tmp_path / "response.sse").exists()
    diagnostic = json.loads((tmp_path / "failure.json").read_bytes())["diagnostic"]["verifier"]
    assert diagnostic["stack"]
    if authorization == b"{":
        assert diagnostic["error_type"] == "SyntaxError"
