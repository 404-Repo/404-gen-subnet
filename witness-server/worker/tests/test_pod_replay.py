"""New Pod evidence can reverify the original capture without live inference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_tls as tls_fixture
from bittensor_wallet import Keypair
from channel import Channel
from storage import Store
from witness_client.calls import request_digest
from worker import Verifier, relay


def test_pod_authorization_snapshot_replays_the_captured_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: tuple[Keypair, str]
) -> None:
    live_relay = relay
    expected = json.dumps({"model": "fixture", "input": "x" * (1024 * 1024)}).encode()

    def capture(
        store: Store,
        prefix: str,
        deadline: float,
        directory: Path,
        verifier: Verifier,
        channel: Channel,
        *,
        round_number: str,
        hotkey: str,
        host: str,
        port: int,
    ) -> dict[str, object]:
        def bind(body: bytes) -> None:
            assert request_digest(body) == request_digest(expected)

        result = live_relay(
            store,
            prefix,
            deadline,
            directory,
            verifier,
            channel,
            round_number=round_number,
            hotkey=hotkey,
            host=host,
            port=port,
            before_release=bind,
        )
        manifest = store.get(prefix + "/manifest.json")
        assert manifest is not None
        (directory / "manifest.json").write_bytes(manifest)
        original = (directory / "response.sse").read_bytes()
        replayed = verifier("replay", directory)
        assert replayed["request_sha256"] == result["request_sha256"]
        assert replayed["response_sha256"] == result["response_sha256"]
        assert (directory / "response.sse").read_bytes() == original
        return result

    monkeypatch.setattr(tls_fixture, "relay", capture)
    tls_fixture.test_real_tls_gate_records_large_exchange_even_when_miner_leaves(
        tmp_path, False, True, 200, "completed", "api", identity
    )
