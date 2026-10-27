"""Recovery polling retains exchange identity and bounded delivery deadlines."""

from __future__ import annotations

import json
import time
import uuid
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import ExchangePending, WitnessError
from witness_client import delivery
from witness_client.gateway import GatewayClient
from witness_client.pods import PodGatewayClient


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
        assert request.url.path == f"/v1/exchanges/{identifier}/progress"
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


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("date", [False, True])
@pytest.mark.parametrize("window", [15, 60])
def test_delivery_respects_retry_after_within_its_deadline(
    clock: list[float], status: int, date: bool, window: int,
) -> None:
    calls: list[float] = []
    hint = formatdate(time.time() + 30, usegmt=True) if date else "30"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock[0])
        if len(calls) == 1:
            return httpx.Response(status, json={"error": "busy"}, headers={"Retry-After": hint})
        return httpx.Response(200, content=b"ready")

    with httpx.Client(base_url="https://witness.test", transport=httpx.MockTransport(handler)) as http:
        def operation() -> bytes:
            return delivery.request(http, "GET", "/v1/pods/current", headers={})

        if window < 30:
            with pytest.raises(WitnessError) as failure:
                delivery.retry(operation, deadline=window)
            assert failure.value.retry_after == 30
            assert calls == [0] and clock[0] == 0
        else:
            assert delivery.retry(operation, deadline=window) == b"ready"
            assert calls == [0, 30]


@pytest.mark.parametrize("blocking", [False, True])
def test_queued_connection_keeps_the_accepted_id_within_its_deadline(
    tmp_path: Path, clock: list[float], blocking: bool,
) -> None:
    accepted = "00000000-0000-4000-8000-000000000001"
    posted: list[str] = []
    polls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/pods/current":
            return httpx.Response(200, json={"round": "test-1", "session": None})
        if request.method == "POST":
            posted.append(json.loads(request.content)["start_id"])
        else:
            assert request.url.path == f"/v1/pods/{accepted}"
            polls.append(clock[0])
        if blocking:
            clock[0] += int(request.headers["Prefer"].removeprefix("wait="))
        return httpx.Response(202 if request.method == "POST" else 200, json={
            "round": "test-1", "session": {"start_id": accepted, "replaces": None, "phase": "queued"},
            "witness": None, "retry_after": 0 if blocking else 30,
        })

    with httpx.Client(base_url="https://witness.test", transport=httpx.MockTransport(handler)) as http:
        gateway = PodGatewayClient(http, Keypair.create_from_uri("//Alice"),
                                   tmp_path, 65)
        try:
            with pytest.raises(WitnessError) as failure:
                gateway.ensure_pod()
            assert failure.value.code == "pod_provisioning_pending"
            expected = [20, 40, 60] if blocking else [30, 60]
            assert polls == expected and clock[0] == 65
            gateway.delivery_timeout = 15
            with pytest.raises(WitnessError, match="remains unresolved"):
                gateway.ensure_pod()
            assert clock[0] == 80 and polls == expected
        finally:
            gateway.close()
    assert len(posted) == 2 and posted[0] != accepted and posted[1] == accepted
    saved = json.loads(next(tmp_path.glob("pod-connect-*.json")).read_bytes())
    assert saved["start_id"] == accepted and saved["replaces"] is None


def test_pod_status_outage_uses_the_capture_wait_deadline(
    tmp_path: Path, clock: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from witness_client.gateway import SignedTransport

    identifier = str(uuid.uuid4())
    polls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == f"/v1/calls/{identifier}"
        polls.append(clock[0])
        return httpx.Response(503, json={"error": "witness_unavailable"})

    with httpx.Client(base_url="https://witness.test", transport=httpx.MockTransport(handler)) as http:
        key = Keypair.create_from_uri("//Alice")
        gateway = PodGatewayClient(http, key, tmp_path, 60)
        monkeypatch.setattr(gateway, "_service", lambda **kwargs: SignedTransport(http, key, 60))
        try:
            with pytest.raises(WitnessError) as failure:
                gateway.snapshot(identifier, deadline=3)
            assert failure.value.code == "capture_timeout"
            assert polls == [0, 1, 2] and clock[0] == 3
        finally:
            gateway.close()


@pytest.mark.parametrize("transport_timeout", [False, True])
def test_status_reconnection_keeps_the_capture_deadline(
    tmp_path: Path, clock: list[float], transport_timeout: bool,
) -> None:
    accepted = str(uuid.uuid4())
    requests: list[str] = []
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        timeouts.extend(request.extensions["timeout"].values())
        if transport_timeout:
            clock[0] += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("Fixture allocation response timed out", request=request)
        clock[0] += int(request.headers["Prefer"].removeprefix("wait="))
        return httpx.Response(202, json={
            "round": "test-1", "session": {"phase": "queued", "start_id": accepted, "replaces": None},
            "witness": None, "retry_after": 0,
        })

    with httpx.Client(base_url="https://witness.test", transport=httpx.MockTransport(handler), timeout=30) as http:
        gateway = PodGatewayClient(http, Keypair.create_from_uri("//Alice"), tmp_path, 60)
        try:
            with pytest.raises((WitnessError, httpx.TimeoutException)):
                gateway.snapshot(str(uuid.uuid4()), deadline=1)
        finally:
            gateway.close()
    assert clock[0] == 1 and requests == ["/v1/pods/current"]
    assert timeouts and all(0 < timeout <= 1 for timeout in timeouts)
