"""Queue failures must recover the same exchange without infrastructure secrets."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.gateway import GatewayClient, encode
from witness_client.types import JSONObject, Session

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"


@pytest.mark.parametrize("provider", ["api", "codex"])
def test_queue_retries_same_job_with_fresh_signatures(
    tmp_path: Path,
    provider: Literal["api", "codex"],
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    calls: list[httpx.Request] = []
    jobs: list[JSONObject] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers = request.headers
        payload = "\n".join(
            [
                "btauth/1",
                "sr25519",
                request.method,
                request.url.path,
                hashlib.sha256(request.content).hexdigest(),
                headers["x-bittensor-nonce"],
                key.ss58_address,
                "404-witness",
            ]
        )
        assert key.verify(
            payload.encode(), bytes.fromhex(headers["x-bittensor-signature"][2:])
        )
        assert headers["x-bittensor-receiver"] == "404-witness"
        assert "authorization" not in headers
        calls.append(request)
        if request.url.path == "/run":
            job: JSONObject = json.loads(request.content)
            assert "round" not in job
            assert job["receiver"] == "404-witness"
            assert job["provider"] == provider
            signed = {name: value for name, value in job.items() if name != "signature"}
            signature = bytes.fromhex(str(job["signature"]))
            assert key.verify(b"404-witness-queue/v1\n" + encode(signed), signature)
            changed = {**signed, "provider": "codex" if provider == "api" else "api"}
            assert not key.verify(
                b"404-witness-queue/v1\n" + encode(changed), signature
            )
            assert (
                tmp_path / str(job["exchange_id"]) / "job.json"
            ).read_bytes() == request.content
            jobs.append(job)
            if len(jobs) == 1:
                raise httpx.ReadError("Lost enqueue acknowledgement")
            return httpx.Response(
                200,
                json={**job, "queue_state": "queued", "round": "1", "session": None},
            )
        if request.url.path == f"/status/{jobs[0]['exchange_id']}":
            return httpx.Response(
                200,
                json={
                    **jobs[0],
                    "queue_state": "connected",
                    "round": "1",
                    "session": {
                        "id": jobs[0]["exchange_id"],
                        "protocol": 2,
                        "provider": (
                            "https://api.openai.com/v1/responses"
                            if provider == "api"
                            else "https://chatgpt.com/backend-api/codex/responses"
                        ),
                        "proof_key_sha256": "a" * 64,
                        "round": "1",
                        "miner": key.ss58_address,
                    },
                },
            )
        raise AssertionError(request.url)

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        gateway.queue_and_wait_for_worker(provider)
    assert jobs[0] == jobs[1]
    assert len({call.content for call in calls if call.url.path == "/run"}) == 1
    assert len({call.headers["x-bittensor-nonce"] for call in calls}) == len(calls)


@pytest.mark.parametrize(
    "field", ["id", "protocol", "provider", "proof_key_sha256", "round", "miner"]
)
@pytest.mark.parametrize("missing", [False, True])
def test_invalid_worker_session_is_rejected_during_worker_startup(
    tmp_path: Path, field: str, missing: bool
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = ""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal identifier
        calls.append(request.url.path)
        if request.url.path == "/run":
            job: JSONObject = json.loads(request.content)
            identifier = str(job["exchange_id"])
            return httpx.Response(
                200,
                json={**job, "queue_state": "queued", "round": "1", "session": None},
            )
        assert request.url.path == f"/status/{identifier}"
        session: dict[str, object] = {
            "id": identifier,
            "protocol": 2,
            "provider": "https://api.openai.com/v1/responses",
            "proof_key_sha256": "a" * 64,
            "round": "1",
            "miner": key.ss58_address,
        }
        if missing:
            del session[field]
        else:
            session[field] = True if field == "protocol" else None
        return httpx.Response(
            200,
            json={
                "exchange_id": identifier,
                "hotkey": key.ss58_address,
                "round": "1",
                "deadline": int(time.time()) + 60,
                "queue_state": "connected",
                "session": session,
            },
        )

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(handler)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        with pytest.raises(WitnessError) as failure:
            gateway.queue_and_wait_for_worker("api")

    assert failure.value.source == "gateway"
    assert failure.value.code == "invalid_gateway_response"
    assert failure.value.exchange_id == identifier
    assert calls == ["/run", f"/status/{identifier}"]


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
    (directory / "job.json").write_bytes(encode({"deadline": int(time.time()) + 300}))
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


def test_unknown_provider_is_rejected_before_job_signing_or_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())

    def unexpected_job() -> uuid.UUID:
        pytest.fail("An invalid provider must not allocate or sign a job")

    def unexpected_network(request: httpx.Request) -> httpx.Response:
        pytest.fail("An invalid provider must not contact the gateway")

    monkeypatch.setattr("witness_client.gateway.uuid.uuid4", unexpected_job)
    with httpx.Client(
        base_url="https://witness.test",
        transport=httpx.MockTransport(unexpected_network),
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        with pytest.raises(ValueError, match="provider"):
            gateway.queue_and_wait_for_worker(cast(Literal["api", "codex"], "other"))

    assert not list(tmp_path.iterdir())


def test_queue_recovery_downloads_retained_answer_without_enqueue(
    tmp_path: Path,
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }
    session: Session = {
        "id": identifier,
        "protocol": 2,
        "provider": "https://api.openai.com/v1/responses",
        "proof_key_sha256": hashlib.sha256(
            (PARAMS / "verifying.bin").read_bytes()
        ).hexdigest(),
        "round": "1",
        "miner": key.ss58_address,
    }
    calls: list[str] = []
    response = (
        b"data: "
        + encode(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "recovered"}],
                        }
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                },
            }
        )
        + b"\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        calls.append(request.url.path)
        if request.url.path.endswith("/progress"):
            return httpx.Response(
                200, json={"status": "issued", "evidence_expires": 9_999_999_999_999}
            )
        assert request.url.path.endswith("/response.sse")
        return httpx.Response(200, content=response)

    with WitnessClient(config, openai_api_key="x" * 80, hotkey=key) as client:
        directory = client._state / identifier
        directory.mkdir(mode=0o700)
        (directory / "session.json").write_bytes(encode(session))
        (directory / "claim.json").write_bytes(b"{}")
        with httpx.Client(
            base_url=client._http.base_url, transport=httpx.MockTransport(handler)
        ) as http:
            client._gateway.http = http
            result = client.resume(identifier)
            assert result.output_text == "recovered"
            assert result.hotkey == key.ss58_address
            assert result.usage["total_tokens"] == 4
            assert client.resume(identifier) == result
    assert len([path for path in calls if path.endswith("/response.sse")]) == 1
    assert not any("/run" in path for path in calls)


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
def test_queue_does_not_retry_permanent_rejections(
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


@pytest.mark.parametrize("operation", ["queue", "recovery", "response"])
def test_gateway_rejection_keeps_identity_across_public_operations(
    tmp_path: Path, operation: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    identifier = str(uuid.uuid4())
    calls: list[httpx.Request] = []

    def rejected(request: httpx.Request) -> httpx.Response:
        nonlocal identifier
        calls.append(request)
        if operation == "queue":
            identifier = json.loads(request.content)["exchange_id"]
        return httpx.Response(403, json={"error": "hotkey_not_registered"})

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(rejected)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 5)
        with pytest.raises(WitnessError) as failure:
            if operation == "queue":
                gateway.queue_and_wait_for_worker("api")
            elif operation == "recovery":
                gateway.wait_for_evidence(identifier)
            else:
                gateway.response(identifier)

    assert len(calls) == 1
    assert failure.value.exchange_id == identifier
    assert failure.value.source == "gateway"
    assert failure.value.status_code == 403
    assert failure.value.code == "hotkey_not_registered"


def test_queue_retry_exhaustion_preserves_gateway_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    clock = [0.0]
    jobs: list[bytes] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("witness_client.delivery.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("witness_client.delivery.time.sleep", sleep)
    monkeypatch.setattr(
        "witness_client.delivery.random.uniform", lambda _low, _high: 1.0
    )

    def unavailable(request: httpx.Request) -> httpx.Response:
        jobs.append(request.content)
        assert len(jobs) <= 3
        return httpx.Response(503, json={"error": "gateway_unavailable"})

    with httpx.Client(
        base_url="https://witness.test", transport=httpx.MockTransport(unavailable)
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 2)
        with pytest.raises(WitnessError) as failure:
            gateway.queue_and_wait_for_worker("api")

    assert len(jobs) == 3 and clock[0] == 2
    assert len(set(jobs)) == 1
    assert failure.value.exchange_id == json.loads(jobs[0])["exchange_id"]
    assert failure.value.source == "gateway"
    assert failure.value.status_code == 503
    assert failure.value.code == "gateway_unavailable"


def test_queue_missing_object_is_distinct_from_missing_job(tmp_path: Path) -> None:
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
        pytest.fail("Invalid queue configuration must not open an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
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


def test_lost_queue_reply_reports_inspection_not_result_recovery(
    tmp_path: Path,
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }
    jobs: list[bytes] = []

    def lost_reply(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/run"
        jobs.append(request.content)
        raise httpx.ReadError("Lost queue reply")

    with (
        WitnessClient(
            config, openai_api_key="x" * 80, hotkey=key, delivery_timeout=0.001
        ) as client,
        httpx.Client(
            base_url=client._http.base_url, transport=httpx.MockTransport(lost_reply)
        ) as http,
    ):
        client._gateway.http = http
        with pytest.raises(WitnessError, match="release") as failure:
            client.create(stream=True, model="fixture", input="hello")
        identifier = failure.value.exchange_id
        assert identifier is not None
        assert failure.value.code == "queue_incomplete"
        assert failure.value.source == "gateway"
        assert failure.value.status_code is None
        assert (client._state / identifier / "job.json").read_bytes() == jobs[0]
        assert len(set(jobs)) == 1  # Any delivery retry has the same immutable job.
        calls = len(jobs)
        with pytest.raises(WitnessError, match="release") as recovery:
            client.resume(identifier)
        assert recovery.value.code == "queue_incomplete"
        assert recovery.value.exchange_id == identifier
        assert len(jobs) == calls  # Recovery cannot enqueue a replacement.


@pytest.mark.parametrize(
    "protocol,expected",
    [
        (None, "recovery_unavailable"),
        (2, "exchange_not_authorized"),
        (1, "unsupported_protocol"),
    ],
)
def test_incomplete_recovery_state_has_explicit_error(
    tmp_path: Path, protocol: int | None, expected: str
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }
    identifier = str(uuid.uuid4())
    with WitnessClient(config, openai_api_key="x" * 80, hotkey=key) as client:
        directory = client._state / identifier
        directory.mkdir(mode=0o700)
        if protocol is not None:
            (directory / "session.json").write_bytes(
                encode(
                    {
                        "protocol": protocol,
                        "round": "1",
                        "provider": "https://api.openai.com/v1/responses",
                        "miner": key.ss58_address,
                    }
                )
            )
        with pytest.raises(WitnessError) as failure:
            client.resume(identifier)
        assert failure.value.code == expected
        assert failure.value.exchange_id == identifier


@pytest.mark.parametrize("status", ["retired", "finished"])
@pytest.mark.parametrize("has_session", [False, True])
def test_create_identifies_worker_failure_before_session_as_gateway(
    tmp_path: Path, status: str, has_session: bool
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }
    identifier = ""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal identifier
        calls.append(request.url.path)
        if request.url.path == "/run":
            job: JSONObject = json.loads(request.content)
            identifier = str(job["exchange_id"])
            return httpx.Response(
                200,
                json={**job, "queue_state": "queued", "round": "1", "session": None},
            )
        assert request.url.path == f"/status/{identifier}"
        return httpx.Response(
            200,
            json={
                "exchange_id": identifier,
                "hotkey": key.ss58_address,
                "round": "1",
                "deadline": int(time.time()) + 60,
                "queue_state": status,
                "session": (
                    {
                        "id": identifier,
                        "protocol": 2,
                        "provider": "https://api.openai.com/v1/responses",
                        "proof_key_sha256": "a" * 64,
                        "round": "1",
                        "miner": key.ss58_address,
                    }
                    if has_session
                    else None
                ),
            },
        )

    with (
        WitnessClient(config, openai_api_key="x" * 80, hotkey=key) as client,
        httpx.Client(
            base_url=client._http.base_url, transport=httpx.MockTransport(handler)
        ) as http,
    ):
        client._gateway.http = http
        with pytest.raises(WitnessError, match="worker stopped") as failure:
            client.create(stream=True, model="fixture", input="hello")

    assert calls == ["/run", f"/status/{identifier}"]
    assert failure.value.exchange_id == identifier
    assert failure.value.source == "gateway"
    assert failure.value.code == "queue_incomplete"


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


def test_busy_allocation_error_identifies_the_request_to_release(
    tmp_path: Path,
) -> None:
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    existing = str(uuid.uuid4())
    with httpx.Client(
        base_url="https://witness.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                409, json={"error": "active_request_exists", "exchange_id": existing}
            )
        ),
    ) as http:
        gateway = GatewayClient(http, key, tmp_path, 1)
        with pytest.raises(WitnessError) as failure:
            gateway.queue_and_wait_for_worker("api")
    assert failure.value.exchange_id == existing
    assert failure.value.code == "active_request_exists"


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
