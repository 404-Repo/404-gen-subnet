"""Subscription credentials must survive queueing, without repeating inference."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock

import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.auth import subscription_credentials
from witness_client.tunnel import WebSocketTunnel
from witness_client.types import Session

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"


def login(home: Path, expiry: float, account: str = "abc123") -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    token = "fixture." + payload + "." + "s" * 64
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": token, "account_id": account},
            }
        )
    )
    return token


def test_refresh_must_supply_enough_time_for_proof_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(tmp_path, time.time() + 120)
    monkeypatch.setattr("witness_client.auth._refresh_codex", lambda *args: None)
    with pytest.raises(ValueError, match="expires too soon"):
        subscription_credentials(tmp_path)


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_subscription_refresh_after_queue_wait_preserves_single_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh_fails: bool
) -> None:
    now = time.time()
    clock = [now]
    monkeypatch.setattr("witness_client.auth.time.time", lambda: clock[0])
    home = tmp_path / "codex"
    home.mkdir()
    old = login(home, now + 600)
    new = ""
    refreshes = opens = exchanges = releases = 0
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    config: WitnessConfig = {
        "witness_url": "https://witness.example",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
        "codex_home": str(home),
    }
    identifier = str(uuid.uuid4())

    def refresh(home: Path, executable: str) -> None:
        nonlocal refreshes, new
        refreshes += 1
        if refresh_fails:
            raise RuntimeError("Fixture refresh unavailable")
        new = login(home, clock[0] + 3600, "def456")

    monkeypatch.setattr("witness_client.auth._refresh_codex", refresh)
    with WitnessClient(config, subscription="codex", hotkey=key) as client:

        def ready(provider: Literal["api", "codex"]) -> Session:
            nonlocal opens
            assert provider == "codex"
            opens += 1
            clock[0] += 601
            directory = client._state / identifier
            directory.mkdir(mode=0o700)
            (directory / "job.json").write_text(
                json.dumps({"deadline": clock[0] + 1800})
            )
            return {
                "id": identifier,
                "protocol": 2,
                "provider": "https://chatgpt.com/backend-api/codex/responses",
                "proof_key_sha256": client._prover.verifying_key_sha256,
                "round": "1",
                "miner": key.ss58_address,
            }

        def exchange(
            tunnel: WebSocketTunnel,
            host: str,
            chunks: Sequence[tuple[bool, bytes]],
            **kwargs: object,
        ) -> bytes:
            nonlocal exchanges
            exchanges += 1
            wire = b"".join(data for _, data in chunks)
            assert tunnel.identifier == identifier
            assert new and new.encode() in wire and old.encode() not in wire
            assert b"ChatGPT-Account-Id: def456\r\n" in wire
            assert (
                client._state / identifier / "request.json"
            ).read_bytes() == wire.split(b"\r\n\r\n", 1)[1]
            raise OSError("Stop fixture before inference")

        @contextmanager
        def tunnel(exchange_id: str) -> Iterator[WebSocketTunnel]:
            nonlocal releases
            assert exchange_id == identifier
            releases += 1
            yield WebSocketTunnel(MagicMock(), exchange_id)

        def release(exchange_id: str) -> None:
            nonlocal releases
            assert exchange_id == identifier
            releases += 1

        monkeypatch.setattr(client._gateway, "release", release)
        monkeypatch.setattr(client._gateway, "queue_and_wait_for_worker", ready)
        monkeypatch.setattr("witness_client.client.exchange", exchange)
        monkeypatch.setattr(client._gateway, "tunnel", tunnel)
        with pytest.raises(WitnessError) as failure:
            client.create(stream=True, model="fixture", input="test")
        assert failure.value.exchange_id == identifier
        assert refreshes == opens == 1
        assert exchanges == (0 if refresh_fails else 1)
        assert releases == 1
        for file in (client._state / identifier).iterdir():
            assert old.encode() not in file.read_bytes()
            if new:
                assert new.encode() not in file.read_bytes()
