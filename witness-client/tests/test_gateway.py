"""Gateway delivery failures retain exchange identity and service errors."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.gateway import GatewayClient

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b"null",
        b"{}",
        b'{"status":null}',
        b'{"status":"running","authorized":"false"}',
        b'{"status":"running","authorized":1}',
        b'{"status":"running","authorized":null}',
        b'{"status":"running","handshook":"false"}',
        b'{"status":"running","handshook":1}',
        b'{"status":"running","handshook":null}',
        b'{"status":"issued","provider_status":"200"}',
        b'{"status":"issued","provider_status":true}',
        b'{"status":"issued","provider_status":200.0}',
        b'{"status":"issued","evidence_expires":"9999999999999"}',
        b'{"status":"issued","evidence_expires":true}',
        b'{"status":"issued","evidence_expires":1.5}',
    ],
)
def test_invalid_worker_status_cannot_accept_recovery(
    tmp_path: Path, raw: bytes
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    directory = tmp_path / identifier
    directory.mkdir()
    (directory / "job.json").write_bytes(
        json.dumps({"deadline": int(time.time()) + 300}).encode()
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "Malformed status must not upload a claim"
        assert request.url.path == f"/exchanges/{identifier}/progress"
        calls.append(request.url.path)
        return httpx.Response(200, content=raw)

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        with pytest.raises(WitnessError) as failure:
            gateway.wait_for_evidence(identifier)

    assert failure.value.source == "gateway"
    assert failure.value.code == "invalid_gateway_response"
    assert failure.value.exchange_id == identifier
    assert calls == [f"/exchanges/{identifier}/progress"]


@pytest.mark.parametrize(
    "status",
    [
        {"status": "running", "authorized": False},
        {"status": "running", "handshook": True, "authorized": False},
        {"status": "running", "handshook": True, "authorized": True},
        {
            "status": "issued",
            "authorized": True,
            "provider_status": 200,
            "evidence_expires": 9_999_999_999_999,
            "usage": {"total_tokens": 4},
            "usage_state": "recorded",
        },
        {"status": "failed", "error_type": "TimeoutError"},
        {"status": "new", "provider_status": None, "evidence_expires": None},
    ],
)
def test_worker_status_fields_and_extra_metadata_are_preserved(
    tmp_path: Path, status: dict[str, object]
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    with httpx.Client(
        base_url="https://witness.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=status)),
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        assert gateway.status(str(uuid.uuid4())) == status


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "invalid_signature"),
        (403, "hotkey_not_registered"),
        (409, "object_conflict"),
        (413, "upload_limit"),
        (429, "job_limit_reached"),
    ],
)
def test_gateway_does_not_retry_permanent_rejections(
    tmp_path: Path, status: int, code: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": code})

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        with pytest.raises(WitnessError, match=code):
            gateway.get(str(uuid.uuid4()), "progress")
    assert calls == 1


@pytest.mark.parametrize("operation", ["recovery", "response"])
def test_gateway_rejection_keeps_identity_across_public_operations(
    tmp_path: Path, operation: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    calls: list[httpx.Request] = []

    def rejected(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"error": "hotkey_not_registered"})

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(rejected)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        with pytest.raises(WitnessError) as failure:
            if operation == "recovery":
                gateway.wait_for_evidence(identifier)
            else:
                gateway.response(identifier)

    assert len(calls) == 1
    assert failure.value.exchange_id == identifier
    assert failure.value.source == "gateway"
    assert failure.value.status_code == 403
    assert failure.value.code == "hotkey_not_registered"


def test_missing_object_is_distinct_from_missing_job(tmp_path: Path) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    for code in ["object_not_ready", "unknown_job"]:
        with httpx.Client(
            base_url="https://witness.test",
            transport=httpx.MockTransport(
                lambda request, code=code: httpx.Response(404, json={"error": code})
            ),
        ) as http:
            gateway = GatewayClient(http, key, tmp_path, 5)
            if code == "object_not_ready":
                assert gateway.get(str(uuid.uuid4()), "progress") is None
            else:
                with pytest.raises(WitnessError, match=code):
                    gateway.get(str(uuid.uuid4()), "progress")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_delivery_timeout_is_rejected_before_creating_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid delivery configuration must not open an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "round": "1",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }
    with pytest.raises(ValueError, match="delivery_timeout"):
        WitnessClient(
            config,
            hotkey=Keypair.create_from_mnemonic(Keypair.generate_mnemonic()),
            openai_api_key="x" * 80,
            delivery_timeout=timeout,
        )
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("operation", ["pending", "rejected", "missing_response"])
def test_remote_exchange_state_errors_keep_gateway_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    clock = [0.0]
    calls = 0

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("witness_client.delivery.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("witness_client.delivery.time.sleep", sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 3
        if operation == "missing_response":
            assert request.url.path.endswith("/response.sse")
            return httpx.Response(404, json={"error": "object_not_ready"})
        assert request.url.path.endswith("/progress")
        return httpx.Response(
            200, json={"status": "running" if operation == "pending" else "failed"}
        )

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 1)
        with pytest.raises(WitnessError) as failure:
            if operation == "missing_response":
                gateway.response(identifier)
            else:
                gateway.wait_for_evidence(identifier)

    assert failure.value.exchange_id == identifier
    assert failure.value.source == "gateway"
    assert calls == (3 if operation == "pending" else 1)
    assert clock[0] == (1 if operation == "pending" else 0)


def test_upgrade_failure_is_a_gateway_error_without_an_automatic_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    connect = MagicMock(side_effect=ConnectionError("Lost upgrade reply"))
    monkeypatch.setattr("witness_client.gateway.connect", connect)
    with httpx.Client(base_url="https://witness.test") as http:
        gateway = GatewayClient(http, key, tmp_path, 1)
        with pytest.raises(WitnessError) as failure, gateway.tunnel(identifier):
            pytest.fail("A rejected upgrade must not start TLS")
    assert failure.value.exchange_id == identifier
    assert failure.value.source == "gateway"
    assert failure.value.code == "connection_lost"
    connect.assert_called_once()
