"""Miner-controlled pauses cannot consume the full queue lifetime on a worker."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bittensor_wallet import Keypair
from conftest import MemoryStore, connected_channels

import worker
from identity import miner_prefix
from storage import json_bytes


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


@pytest.mark.parametrize("stage", ["hello", "authorization"])
@pytest.mark.parametrize("trickle", [False, True])
@pytest.mark.parametrize("remaining", [30, 7 * 86400])
def test_miner_cannot_extend_setup_with_silence_or_partial_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
    stage: str,
    trickle: bool,
    remaining: int,
) -> None:
    hotkey, _ = identity
    prefix = f"{miner_prefix('1', hotkey.ss58_address)}/{uuid.uuid4()}"
    clock = Clock()
    started = clock.now
    monkeypatch.setattr(worker, "time", clock)
    provider = MagicMock()
    monkeypatch.setattr(
        worker, "socket", SimpleNamespace(create_connection=lambda *a, **kw: provider)
    )
    monkeypatch.setattr(
        worker, "select", SimpleNamespace(select=lambda *args: ([], [], []))
    )
    received = 0
    hello = b"\x16\x03\x03\x00\x01x"

    def receive(deadline: float) -> bytes:
        nonlocal received
        received += 1
        assert received < 10_000, "Worker kept accepting miner traffic indefinitely"
        if stage == "authorization" and received == 1:
            return hello
        if trickle:
            clock.now = min(deadline, clock.now + 10)
            return b"\x16"  # Incomplete ClientHello or unproved request bytes.
        clock.now = deadline
        raise TimeoutError

    channel = MagicMock()
    channel.recv.side_effect = receive
    channel.claim.return_value = None
    verifier = MagicMock(return_value={"released": len(hello)})
    store = MemoryStore()
    with pytest.raises(TimeoutError):
        worker.relay(
            store,
            prefix,
            started + remaining,
            tmp_path,
            verifier,
            channel,
            round_number="1",
            hotkey=hotkey.ss58_address,
            host="provider.test",
        )
    expected = (
        worker.CLIENT_HELLO_TIMEOUT
        if stage == "hello"
        else worker.AUTHORIZATION_TIMEOUT
    )
    assert clock.now == pytest.approx(started + min(expected, remaining))
    assert store.objects == {}
    if stage == "hello":
        verifier.assert_not_called()
        provider.__enter__.return_value.sendall.assert_not_called()
    else:
        provider.__enter__.return_value.sendall.assert_called_once_with(hello)
    channel.finish.assert_called_once()
    provider.__exit__.assert_called_once()


@pytest.mark.parametrize("late_proof,capture_elapsed", [(True, None), (False, None), (True, 45), (False, 45), (False, 61)])
def test_setup_deadline_applies_before_release_but_not_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
    late_proof: bool,
    capture_elapsed: int | None,
) -> None:
    hotkey, _ = identity
    prefix = f"{miner_prefix('1', hotkey.ss58_address)}/{uuid.uuid4()}"
    clock = Clock()
    started = clock.now
    monkeypatch.setattr(worker, "time", clock)
    provider = MagicMock()
    connection = provider.__enter__.return_value
    monkeypatch.setattr(
        worker, "socket", SimpleNamespace(create_connection=lambda *a, **kw: provider)
    )
    monkeypatch.setattr(
        worker, "select", SimpleNamespace(select=lambda *args: ([connection], [], []))
    )
    record = b"\x16\x03\x03\x00\x01x"
    reads = 0

    def receive(size: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 3:
            clock.now += capture_elapsed if capture_elapsed is not None else worker.AUTHORIZATION_TIMEOUT
        return record if reads <= 3 else b""

    connection.recv.side_effect = receive
    channel = MagicMock()
    channel.recv.return_value = record
    channel.claim.side_effect = lambda phase: json_bytes(
        {"client_bytes": len(record) * (2 if phase == "handshake" else 3)}
    )

    def verify(mode: str, directory: Path) -> dict[str, object]:
        if mode == "hello":
            return {"released": len(record)}
        if mode == "handshake":
            (directory / "handshake.json").write_bytes(b"{}")
        if mode == "authorize":
            (directory / "authorization.json").write_bytes(b"{}")
            (directory / "request.json").write_bytes(b"{}")
            if late_proof:
                clock.now += worker.AUTHORIZATION_TIMEOUT
            elif capture_elapsed is not None:
                clock.now += 18
        if mode == "finish":
            (directory / "response.sse").write_bytes(b"response")
            return {"provider_status": 200}
        return {}

    store = MemoryStore()

    def run() -> dict[str, object]:
        return worker.relay(
            store,
            prefix,
            None if capture_elapsed is not None else started + 7 * 86400,
            tmp_path,
            verify,
            channel,
            round_number="1",
            hotkey=hotkey.ss58_address,
            host="provider.test",
            before_release=(lambda body: clock.now + 60) if capture_elapsed is not None else None,
        )

    if late_proof:
        with pytest.raises(TimeoutError, match="Request authorization deadline"):
            run()
        assert connection.sendall.call_count == 2  # TLS setup only.
        assert store.get(prefix + "/response.sse") is None
    elif capture_elapsed == 61:
        with pytest.raises(TimeoutError, match="Provider exchange deadline"):
            run()
        assert connection.sendall.call_count == 3
        assert store.get(prefix + "/response.sse") is None
    else:
        assert run()["provider_status"] == 200
        if capture_elapsed is not None:
            assert clock.now == started + 18 + capture_elapsed
        else:
            assert clock.now >= started + worker.AUTHORIZATION_TIMEOUT
        assert connection.sendall.call_count == 3
        assert store.get(prefix + "/response.sse") == b"response"
    channel.finish.assert_called_once()
    provider.__exit__.assert_called_once()


def test_silent_real_websocket_releases_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
) -> None:
    hotkey, _ = identity
    identifier = str(uuid.uuid4())
    prefix = f"{miner_prefix('1', hotkey.ss58_address)}/{identifier}"
    monkeypatch.setattr(worker, "CLIENT_HELLO_TIMEOUT", 0.1)
    provider = MagicMock()
    monkeypatch.setattr(
        worker, "socket", SimpleNamespace(create_connection=lambda *a, **kw: provider)
    )
    verifier = MagicMock()
    with connected_channels(identifier) as (tunnel, channel):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                worker.relay,
                MemoryStore(),
                prefix,
                time.monotonic() + 7 * 86400,
                tmp_path,
                verifier,
                channel,
                round_number="1",
                hotkey=hotkey.ss58_address,
                host="provider.test",
            )
            try:
                with pytest.raises(TimeoutError):
                    future.result(timeout=2)
                assert future.done(), "The worker is still occupied"
                assert tunnel.recv(timeout=1) == b""
            finally:
                tunnel.close()
    verifier.assert_not_called()
    provider.__exit__.assert_called_once()
