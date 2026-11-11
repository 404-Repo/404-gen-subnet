"""Subscription credentials retain enough validity for proof preparation."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from witness_client.auth import subscription_credentials
from witness_client.request import load_credentials
from witness_client.types import WitnessConfig

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


def test_service_credentials_are_reloaded_for_each_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_refresh(*args: object) -> None:
        pytest.fail("Access-only credentials must never refresh the local login")

    monkeypatch.setattr("witness_client.auth._refresh_codex", no_refresh)
    path = tmp_path / "subscription.json"
    config: WitnessConfig = {
        "witness_url": "https://witness.test", "proof_parameters": "unused",
        "codex_credentials_file": str(path),
    }
    first = login(tmp_path, time.time() + 3600, "abc123")
    path.write_text(json.dumps({"access_token": first, "account_id": "abc123"}))
    assert load_credentials(config, "codex", None) == (first, "abc123")
    second = login(tmp_path, time.time() + 7200, "abc123")
    path.write_text(json.dumps({"access_token": second, "account_id": "abc123"}))
    assert load_credentials(config, "codex", None) == (second, "abc123")
    assert first != second


@pytest.mark.parametrize("problem", ["missing", "expired", "refresh_token", "invalid_expiry"])
def test_service_credentials_do_not_fall_back_to_local_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str,
) -> None:
    monkeypatch.setattr("witness_client.auth._refresh_codex", lambda *args: pytest.fail("Unexpected refresh"))
    token = login(tmp_path, time.time() + 3600)
    path = tmp_path / "subscription.json"
    data = {"access_token": token, "account_id": "abc123"}
    if problem == "expired":
        data["access_token"] = login(tmp_path, time.time() + 100)
    elif problem == "refresh_token":
        data["refresh_token"] = "must-stay-on-the-service"
    elif problem == "invalid_expiry":
        data["access_token"] = "invalid"
    if problem != "missing":
        path.write_text(json.dumps(data))
    with pytest.raises((OSError, ValueError)):
        subscription_credentials(tmp_path, credentials_file=path)
