"""Queued workers claim privately before opening provider connections."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bittensor_wallet import Keypair
from conftest import MemoryStore, connected_channels

import worker
from identity import signed_exchange, worker_connection
from storage import CHUNK_BYTES


def test_fragmented_megabyte_stream() -> None:
    identifier = str(uuid.uuid4())
    data = b"ciphertext" * 200_000
    with connected_channels(identifier) as (tunnel, channel):
        tunnel.send(data)
        tunnel.flush()
        result = bytearray()
        while len(result) < len(data):
            chunk = channel.recv(time.monotonic() + 1)
            assert len(chunk) <= CHUNK_BYTES
            result.extend(chunk)
        assert result == data
        channel.send(data)
        channel.finish()
        result.clear()
        while chunk := tunnel.recv(timeout=1):
            result.extend(chunk)
        assert result == data


def test_worker_rejected_claim_never_writes_evidence_or_calls_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: tuple[Keypair, str]
) -> None:
    hotkey, _ = identity
    store = MemoryStore()
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    connect = MagicMock(
        side_effect=ConnectionError("Worker already claimed or retired")
    )
    relay = MagicMock()
    monkeypatch.setattr(worker, "connect", connect)
    monkeypatch.setattr(worker, "relay", relay)
    job: dict[str, object] = {
        "id": "duplicate",
        "input": {
            **signed_exchange(
                hotkey, str(uuid.uuid4()), int(time.time()) + 30, provider="api"
            ),
            "round": "1",
            "expires": int(time.time()) + 30,
            "gateway": "https://gateway.test",
            "token": "a" * 64,
        },
    }
    with pytest.raises(ConnectionError):
        worker.handler(job)
    assert store.objects == {}
    relay.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"token": ""},
        {"token": "not-a-capability"},
        {"gateway": "http://gateway.test"},
        {"gateway": "https://someone@gateway.test"},
        {"gateway": "https://gateway.test/path"},
        {"expires": 0},
        {"expires": True},
        {"expires": 999999999999},
    ],
)
def test_invalid_private_connection_is_rejected(change: dict[str, object]) -> None:
    data: dict[str, object] = {
        "gateway": "https://gateway.test",
        "token": "a" * 64,
        "expires": int(time.time()) + 30,
        "deadline": int(time.time()) + 30,
        "round": "1",
        "hotkey": "fixture",
        "exchange_id": str(uuid.uuid4()),
        **change,
    }
    with pytest.raises(ValueError):
        worker_connection(data)


def test_expired_job_does_not_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, identity: tuple[Keypair, str]
) -> None:
    hotkey, _ = identity
    monkeypatch.setattr(worker, "R2Store", MemoryStore)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    connect = MagicMock()
    monkeypatch.setattr(worker, "connect", connect)
    with pytest.raises(ValueError, match="Expired"):
        worker.handler(
            {
                "id": "old",
                "input": {
                    **signed_exchange(
                        hotkey,
                        str(uuid.uuid4()),
                        int(time.time()) - 1,
                        provider="codex",
                    ),
                    "round": "1",
                },
            }
        )
    connect.assert_not_called()
