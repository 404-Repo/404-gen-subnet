"""Retained evidence rejection is distinct from a failed verifier process."""

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


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("verifier", 180), OSError("verifier unavailable")])
def test_finish_preserves_verifier_infrastructure_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setattr("worker.subprocess.run", MagicMock(side_effect=error))
    with pytest.raises(type(error)):
        verify("finish", tmp_path, provider="api")


@pytest.mark.parametrize("authorization", [None, b"{", b"{}"])
def test_finish_rejects_missing_or_invalid_evidence(tmp_path: Path, authorization: bytes | None) -> None:
    if authorization is not None:
        (tmp_path / "authorization.json").write_bytes(authorization)
    (tmp_path / "server.bin").write_bytes(b"")
    with pytest.raises(ValueError, match="verification rejected"):
        verify("finish", tmp_path, provider="api")
    assert not (tmp_path / "response.sse").exists()
