"""Polling preserves job identity, deadlines, and accepted proof uploads."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessError
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
@pytest.mark.parametrize(
    "queue_state", ["queued", "submitting", "uncertain", "connected"]
)
def test_worker_polling_keeps_one_job_and_deadline_through_outage(
    tmp_path: Path, clock: list[float], ready: bool, queue_state: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    jobs: list[bytes] = []
    job_polls: list[float] = []
    expires = int(time.time()) + 12

    def handler(request: httpx.Request) -> httpx.Response:
        assert clock[0] < 12, "Polling extended the original deadline"
        if request.url.path == "/run":
            jobs.append(request.content)
            return httpx.Response(
                200 if queue_state == "queued" else 202,
                json={
                    **json.loads(request.content),
                    "queue_state": queue_state,
                    "deadline": expires,
                    "round": "1",
                    "session": None,
                },
            )
        identifier = json.loads(jobs[0])["exchange_id"]
        assert request.url.path == f"/status/{identifier}"
        job_polls.append(clock[0])
        if clock[0] == 5:
            return httpx.Response(503, json={"error": "unavailable"})
        worker_ready = ready and clock[0] >= 11
        return httpx.Response(
            200,
            json={
                **json.loads(jobs[0]),
                "queue_state": "connected" if worker_ready else queue_state,
                "round": "1",
                # Later replies must not extend the initially accepted deadline.
                "deadline": expires + 300,
                "session": (
                    {
                        "id": identifier,
                        "protocol": 2,
                        "provider": "https://api.openai.com/v1/responses",
                        "proof_key_sha256": "a" * 64,
                        "round": "1",
                        "miner": key.ss58_address,
                    }
                    if worker_ready
                    else None
                ),
            },
        )

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 6)
        if ready:
            session = gateway.queue_and_wait_for_worker("api")
            assert session["id"] == json.loads(jobs[0])["exchange_id"]
        else:
            with pytest.raises(WitnessError) as failure:
                gateway.queue_and_wait_for_worker("api")
            assert failure.value.exchange_id == json.loads(jobs[0])["exchange_id"]
            assert failure.value.code == "queue_incomplete"
            assert failure.value.source == "gateway"
            assert clock[0] == 12

    assert len(jobs) == 1
    assert job_polls == [0, 5, 6, 11]


def test_session_arriving_after_deadline_is_not_used(
    tmp_path: Path, clock: list[float]
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    expires = int(time.time()) + 1
    jobs: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/run":
            jobs.append(request.content)
            return httpx.Response(
                200,
                json={
                    **json.loads(request.content),
                    "queue_state": "queued",
                    "deadline": expires,
                    "round": "1",
                    "session": None,
                },
            )
        job = json.loads(jobs[0])
        assert request.url.path == f"/status/{job['exchange_id']}"
        clock[0] = 2
        return httpx.Response(
            200,
            json={
                **job,
                "queue_state": "connected",
                "deadline": expires,
                "round": "1",
                "session": {
                    "id": job["exchange_id"],
                    "protocol": 2,
                    "provider": "https://api.openai.com/v1/responses",
                    "proof_key_sha256": "a" * 64,
                    "round": "1",
                    "miner": key.ss58_address,
                },
            },
        )

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 6)
        with pytest.raises(WitnessError, match="deadline reached") as failure:
            gateway.queue_and_wait_for_worker("api")
    assert len(jobs) == 1
    assert failure.value.code == "queue_incomplete"
    assert failure.value.exchange_id == json.loads(jobs[0])["exchange_id"]


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
            with pytest.raises(WitnessError) as failure:
                gateway.wait_for_evidence(identifier)
            assert failure.value.exchange_id == identifier
            assert failure.value.code == "capture_in_progress"
            assert failure.value.source == "gateway"
            assert polls == [0, 0.5, 1, 2, 2.5, 3]
