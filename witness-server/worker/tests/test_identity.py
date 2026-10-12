from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bittensor_wallet import Keypair
from conftest import MemoryStore

import worker
from channel import Channel
from identity import (
    Provider,
    authenticate_exchange,
    miner_prefix,
    payload,
    signed_exchange,
)


def test_round_and_hotkey_define_the_transcript_group(
    identity: tuple[Keypair, str],
) -> None:
    hotkey, other = identity
    first = miner_prefix("1", hotkey.ss58_address)
    assert first == f"rounds/1/{hotkey.ss58_address}"
    assert first != miner_prefix("2", hotkey.ss58_address)
    assert miner_prefix("0", hotkey.ss58_address) == f"rounds/0/{hotkey.ss58_address}"
    assert miner_prefix("test-0", hotkey.ss58_address) == (
        f"rounds/test-0/{hotkey.ss58_address}"
    )
    assert first != miner_prefix("1", other)
    with pytest.raises(ValueError):
        miner_prefix("../1", hotkey.ss58_address)
    with pytest.raises(ValueError):
        miner_prefix("1", "../another-miner")


@pytest.mark.parametrize("round_number", ["1", "test-0"])
def test_job_signature_survives_queue_delay(
    identity: tuple[Keypair, str], monkeypatch: pytest.MonkeyPatch, round_number: str
) -> None:
    hotkey, _ = identity
    started = int(time.time())
    identifier = str(uuid.uuid4())
    signed = signed_exchange(hotkey, identifier, started + 1800, provider="codex")
    assert "round" not in signed
    with pytest.raises(ValueError, match="Invalid signed exchange"):
        authenticate_exchange(signed, round_number)
    data = {**signed, "round": round_number}
    assert hotkey.verify(payload(data), bytes.fromhex(str(signed["signature"])))
    with pytest.raises(ValueError, match="Wrong witness or round"):
        authenticate_exchange(data, "2")
    assert data["receiver"] == "404-witness"
    monkeypatch.setattr("time.time", lambda: started + 300)
    assert authenticate_exchange(data, round_number) == (
        hotkey.ss58_address,
        identifier,
        "codex",
    )
    monkeypatch.setattr("time.time", lambda: started + 1800)
    with pytest.raises(ValueError, match="Expired"):
        authenticate_exchange(data, round_number)


@pytest.mark.parametrize("receiver", ["another-witness", "miner-hotkey"])
def test_valid_signature_for_another_receiver_never_writes_to_r2(
    receiver: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
) -> None:
    hotkey, _ = identity
    data = {
        **signed_exchange(
            hotkey, str(uuid.uuid4()), int(time.time()) + 60, provider="codex"
        ),
        "round": "1",
    }
    data["receiver"] = hotkey.ss58_address if receiver == "miner-hotkey" else receiver
    data["signature"] = hotkey.sign(payload(data)).hex()
    store = MemoryStore()
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    with pytest.raises(ValueError, match="Wrong witness"):
        worker.handler({"id": "wrong-recipient", "input": data})
    assert store.objects == {}


@pytest.mark.parametrize(
    "round_number", ["1", "test-0", "../1", "test/0", "-test", "t" * 65]
)
def test_health_checks_round_without_a_witness_key(
    round_number: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITNESS_ROUND", round_number)
    monkeypatch.delenv("WITNESS_HOTKEY", raising=False)
    store = MemoryStore()
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    monkeypatch.setattr(worker, "verify", lambda *_: {})
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    if round_number in ("1", "test-0"):
        result = worker.handler({"input": {"operation": "health"}})
        assert result["status"] == "ready"
        assert result["receiver"] == "404-witness"
    else:
        with pytest.raises(ValueError, match="Round must"):
            worker.handler({"input": {"operation": "health"}})
        assert store.writes == []
    assert store.objects == {}


@pytest.mark.parametrize(
    "field",
    ["hotkey", "round", "receiver", "exchange_id", "deadline", "provider", "signature"],
)
def test_forged_identity_never_writes_to_r2(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
) -> None:
    hotkey, _ = identity
    data = {
        **signed_exchange(
            hotkey, str(uuid.uuid4()), int(time.time()) + 60, provider="codex"
        ),
        "round": "1",
    }
    data[field] = {
        "hotkey": identity[1],
        "round": "2",
        "receiver": hotkey.ss58_address,
        "exchange_id": str(uuid.uuid4()),
        "deadline": int(time.time()) + 120,
        "provider": "api",
        "signature": "00" * 64,
    }[field]
    store = MemoryStore()
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    with pytest.raises(ValueError):
        worker.handler({"id": "forged-job", "input": data})
    assert store.objects == {}


@pytest.mark.parametrize(
    "provider", [None, "", "API", "https://example.org", "__proto__", ["api"]]
)
def test_unsupported_provider_never_starts_capture(
    provider: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
) -> None:
    hotkey, _ = identity
    data = {
        **signed_exchange(
            hotkey, str(uuid.uuid4()), int(time.time()) + 60, provider="api"
        ),
        "round": "1",
    }
    data["provider"] = provider
    data["signature"] = hotkey.sign(payload(data)).hex()
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")

    def forbidden_store() -> MemoryStore:
        pytest.fail("Invalid provider must be rejected before accessing storage")

    monkeypatch.setattr(worker, "R2Store", forbidden_store)
    with pytest.raises(ValueError, match="Unknown witness provider"):
        worker.handler({"id": "invalid-provider", "input": data})
    del data["provider"]
    with pytest.raises(ValueError, match="Invalid signed exchange"):
        worker.handler({"id": "missing-provider", "input": data})


def test_same_worker_routes_each_signed_provider_and_binds_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[Keypair, str],
) -> None:
    hotkey, _ = identity
    store = MemoryStore()
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    routed: list[str] = []
    verified: list[tuple[str, Provider | None]] = []

    def verify(
        mode: str, directory: Path, *, provider: Provider | None = None
    ) -> dict[str, object]:
        verified.append((mode, provider))
        return {}

    def relay(
        store: object,
        prefix: str,
        deadline: float,
        directory: Path,
        verifier: worker.Verifier,
        channel: Channel,
        *,
        round_number: str,
        hotkey: str,
        host: str,
    ) -> dict[str, object]:
        routed.append(host)
        for mode in ("hello", "handshake", "authorize", "finish"):
            verifier(mode, directory)
        return {}

    monkeypatch.setattr(worker, "verify", verify)
    monkeypatch.setattr(worker, "relay", relay)
    monkeypatch.setattr(worker, "connect", MagicMock())
    choices: list[tuple[Provider, str]] = [
        ("api", "https://api.openai.com/v1/responses"),
        ("codex", "https://chatgpt.com/backend-api/codex/responses"),
        ("api", "https://api.openai.com/v1/responses"),
    ]
    for provider, url in choices:
        identifier = str(uuid.uuid4())
        data = {
            **signed_exchange(
                hotkey, identifier, int(time.time()) + 60, provider=provider
            ),
            "round": "1",
        }
        assert authenticate_exchange(data, "1") == (
            hotkey.ss58_address,
            identifier,
            provider,
        )
        data.update(
            {
                "gateway": "https://gateway.test",
                "token": "a" * 64,
                "expires": int(time.time()) + 30,
            }
        )
        worker.handler({"id": identifier, "input": data})
        prefix = f"{miner_prefix('1', hotkey.ss58_address)}/{identifier}"
        assert json.loads(store.objects[f"{prefix}/session.json"])["provider"] == url
        assert not any(
            "token" in item or "gateway" in item
            for item in json.loads(store.objects[f"{prefix}/session.json"])
        )
    assert routed == ["api.openai.com", "chatgpt.com", "api.openai.com"]
    assert verified == [
        (mode, provider)
        for provider, _ in choices
        for mode in ("hello", "handshake", "authorize", "finish")
    ]


def test_multiple_requests_keep_distinct_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: tuple[Keypair, str]
) -> None:
    hotkey, _ = identity
    store = MemoryStore()
    monkeypatch.setattr(worker, "R2Store", lambda: store)
    monkeypatch.setattr(worker, "PARAMETERS", tmp_path)
    (tmp_path / "verifying.bin").write_bytes(b"fixture-key")
    observed: list[tuple[str, str, str]] = []

    def relay(
        store: object,
        prefix: str,
        deadline: float,
        directory: Path,
        verifier: object,
        channel: Channel,
        *,
        round_number: str,
        hotkey: str,
        host: str,
    ) -> dict[str, object]:
        observed.append((prefix, round_number, hotkey))
        return {}

    monkeypatch.setattr(worker, "relay", relay)
    monkeypatch.setattr(worker, "connect", MagicMock())
    identifiers = [str(uuid.uuid4()), str(uuid.uuid4())]
    group = miner_prefix("1", hotkey.ss58_address)
    for identifier in identifiers:
        worker.handler(
            {
                "id": identifier,
                "input": {
                    **signed_exchange(
                        hotkey, identifier, int(time.time()) + 60, provider="codex"
                    ),
                    "round": "1",
                    "gateway": "https://gateway.test",
                    "token": "a" * 64,
                    "expires": int(time.time()) + 30,
                },
            }
        )
        session = json.loads(store.objects[f"{group}/{identifier}/session.json"])
        assert session["miner"] == hotkey.ss58_address
        assert session["round"] == "1"
    assert observed == [
        (f"{group}/{identifier}", "1", hotkey.ss58_address)
        for identifier in identifiers
    ]
