"""Recovery polling retains exchange identity and bounded delivery deadlines."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import ExchangePending, WitnessError
from witness_client.gateway import GatewayClient


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("witness_client.delivery.time.monotonic", lambda: now[0])
    monkeypatch.setattr("witness_client.delivery.time.sleep", sleep)
    monkeypatch.setattr(
        "witness_client.delivery.random.uniform", lambda _low, _high: 1.0
    )
    epoch = float(int(time.time()))
    monkeypatch.setattr("witness_client.gateway.time.time", lambda: epoch + now[0])
    return now


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("outage", ["http", "transport"])
def test_recovery_polls_saved_progress_through_outage(
    tmp_path: Path, clock: list[float], ready: bool, outage: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    polls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "Recovery must not start another generation"
        assert request.url.path == f"/exchanges/{identifier}/progress"
        assert clock[0] <= 3, "Polling extended the recovery deadline"
        polls.append(clock[0])
        if clock[0] == 0:
            return httpx.Response(404, json={"error": "object_not_ready"})
        if clock[0] == 1:
            if outage == "transport":
                raise httpx.ReadError("Connection dropped", request=request)
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "status": "issued" if ready and clock[0] >= 2.5 else "running",
                "handshook": True,
                "authorized": True,
            },
        )

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 3)
        if ready:
            assert gateway.wait_for_evidence(identifier)["status"] == "issued"
            assert polls == [0, 0.5, 1, 2, 2.5]
        else:
            with pytest.raises(ExchangePending) as failure:
                gateway.wait_for_evidence(identifier)
            assert failure.value.exchange == identifier
            assert failure.value.exchange_id == identifier
            assert failure.value.code == "capture_in_progress"
            assert failure.value.source == "gateway"
            assert polls == [0, 0.5, 1, 2, 2.5, 3]
