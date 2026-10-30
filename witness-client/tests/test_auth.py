"""Subscription credentials retain enough validity for proof preparation."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from witness_client.auth import subscription_credentials

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
