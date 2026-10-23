"""An issued exchange on a silent stream is recovered, never requested again."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest
from bittensor_wallet import Keypair

from witness_client import WitnessClient, WitnessConfig, WitnessError
from witness_client.transport import TunnelSocket
from witness_client.tunnel import WebSocketTunnel
from witness_client.types import (
    CredentialProof,
    EarlyDisclosure,
    HandshakeDisclosure,
    PrivateRecord,
)

ISSUED_AT = 200.0  # The worker saves evidence 200 s after authorization.
BODY = (
    b"data: "
    + json.dumps(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_recorded",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            },
        }
    ).encode()
    + b"\n\n"
)
RAW = f"HTTP/1.1 200 OK\r\nContent-Length: {len(BODY)}\r\n\r\n".encode() + BODY
DISCLOSURE: HandshakeDisclosure = {
    "disclosures": {"client": [], "server": []},
    "response_key": {"key": "00" * 32, "iv": "00" * 12, "number": 0},
    "client_bytes": 0,
    "probe": True,
}


class Worker:
    """Live stream that acknowledges proofs, then delivers at ISSUED_AT or hangs."""

    def __init__(self, clock: list[float], deliver: bool) -> None:
        self.clock, self.deliver = clock, deliver
        self.incoming: deque[str | bytes] = deque()
        self.sent: list[str | bytes] = []
        self.reads: list[float] = []
        self.closed = False

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def send(self, message: str | bytes) -> None:
        assert not self.closed
        self.sent.append(message)
        if isinstance(message, str):
            phase = json.loads(message)["type"]
            self.incoming.append(
                json.dumps(
                    {
                        "type": "status",
                        "status": "running",
                        "handshook": True,
                        "authorized": phase == "authorize",
                    }
                )
            )

    def recv(self, timeout: float | None = None) -> str | bytes:
        assert not self.closed and timeout is not None
        self.reads.append(timeout)
        if self.deliver and not self.incoming and self.clock[0] + timeout >= ISSUED_AT:
            self.clock[0] = max(self.clock[0], ISSUED_AT)
            self.incoming.extend([RAW, '{"type":"end"}'])
            self.deliver = False
        if self.incoming:
            return self.incoming.popleft()
        self.clock[0] += timeout  # Keepalive pings carry no messages.
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


class Gateway:
    """Gateway fake on the shared clock; status reads fail before ISSUED_AT."""

    def __init__(
        self,
        clock: list[float],
        hotkey: Keypair,
        failure: str | None,
        persistent: bool,
        download_failures: int,
    ) -> None:
        self.clock, self.hotkey = clock, hotkey
        self.failure, self.persistent = failure, persistent
        self.download_failures = download_failures
        self.calls: list[tuple[str, str, float]] = []
        self.identifier = ""
        self.worker: Worker | None = None
        self.connects = self.generations = 0

    def paths(self, suffix: str) -> list[float]:
        return [at for _, path, at in self.calls if path.endswith(suffix)]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, self.clock[0]))
        if path == "/run":
            if self.identifier:
                pytest.fail("A second generation was queued")
            job = json.loads(request.content)
            self.identifier = job["exchange_id"]
            return httpx.Response(
                200,
                json={
                    **job,
                    "queue_state": "queued",
                    "deadline": int(time.time()) + 3600,
                    "round": "1",
                    "session": None,
                },
            )
        if path == f"/status/{self.identifier}":
            return httpx.Response(
                200,
                json={
                    "exchange_id": self.identifier,
                    "hotkey": self.hotkey.ss58_address,
                    "round": "1",
                    "deadline": int(time.time()) + 3600,
                    "queue_state": "connected",
                    "session": {
                        "id": self.identifier,
                        "protocol": 2,
                        "provider": "https://api.openai.com/v1/responses",
                        "proof_key_sha256": "a" * 64,
                        "round": "1",
                        "miner": self.hotkey.ss58_address,
                    },
                },
            )
        if path == f"/exchanges/{self.identifier}/progress":
            assert request.method == "GET"
            if self.clock[0] < ISSUED_AT or self.persistent:
                return self.status_failure(request)
            return httpx.Response(
                200,
                json={
                    "status": "issued",
                    "handshook": True,
                    "authorized": True,
                    "provider_status": 200,
                    "evidence_expires": 9_999_999_999_999,
                },
            )
        if path == f"/exchanges/{self.identifier}/response.sse":
            assert request.method == "GET"
            if self.download_failures:
                self.download_failures -= 1
                return httpx.Response(500, json={"error": "storage_unavailable"})
            return httpx.Response(200, content=BODY)
        pytest.fail(f"Unexpected gateway call {request.method} {path}")

    def status_failure(self, request: httpx.Request) -> httpx.Response:
        if self.failure is None:
            return httpx.Response(
                200, json={"status": "running", "handshook": True, "authorized": True}
            )
        if self.failure == "transport":
            raise httpx.ReadError("Status connection dropped", request=request)
        if self.failure == "invalid":
            return httpx.Response(200, content=b"[]")
        return httpx.Response(
            {"unavailable": 503, "unknown_job": 404, "object_not_ready": 404}.get(
                self.failure, 500
            ),
            json={"error": self.failure},
        )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    epoch = float(int(time.time()))
    monkeypatch.setattr("witness_client.delivery.time.monotonic", lambda: now[0])
    monkeypatch.setattr("witness_client.delivery.time.sleep", sleep)
    monkeypatch.setattr("witness_client.delivery.time.time", lambda: epoch + now[0])
    monkeypatch.setattr(
        "witness_client.delivery.random.uniform", lambda _low, _high: 1.0
    )
    return now


def start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
    *,
    deliver: bool,
    failure: str | None = None,
    persistent: bool = False,
    download_failures: int = 0,
    stall_timeout: float = 600,
) -> tuple[WitnessClient, Gateway]:
    class ProverStub:
        verifying_key_sha256 = "a" * 64

        def __init__(self, parameters: Path) -> None:
            pass

        def prove(self, records: list[PrivateRecord]) -> CredentialProof:
            return {
                "nonce": "00" * 12,
                "bundle": {"commitment": "fixture", "mac_keys": [], "proofs": []},
            }

    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    gateway = Gateway(clock, key, failure, persistent, download_failures)

    def connect(url: str, **kwargs: object) -> Worker:
        assert url == f"wss://witness.test/exchanges/{gateway.identifier}/connect"
        gateway.connects += 1
        gateway.worker = Worker(clock, deliver)
        return gateway.worker

    def exchange(
        tunnel: WebSocketTunnel,
        host: str,
        chunks: Sequence[tuple[bool, bytes]],
        *,
        handshake: Callable[[HandshakeDisclosure], None],
        authorize: Callable[[EarlyDisclosure], None],
        timeout: float,
    ) -> bytes:
        # TLS stand-in: the same proof callbacks and delivery wait, plain bytes.
        gateway.generations += 1
        handshake(DISCLOSURE)
        authorize({**DISCLOSURE, "private": []})
        socket = TunnelSocket(tunnel, timeout)
        raw = bytearray()
        while data := socket.recv(65536):
            raw.extend(data)
        return bytes(raw)

    monkeypatch.setattr("witness_client.client.Prover", ProverStub)
    monkeypatch.setattr("witness_client.gateway.connect", connect)
    monkeypatch.setattr("witness_client.client.exchange", exchange)
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(tmp_path / "parameters"),
        "state_directory": str(tmp_path / "state"),
    }
    client = WitnessClient(
        config, openai_api_key="x" * 80, hotkey=key, stall_timeout=stall_timeout
    )
    client._http.close()
    client._http = httpx.Client(
        base_url=client._http.base_url, transport=httpx.MockTransport(gateway.handle)
    )
    client._gateway.http = client._http
    return client, gateway


def assert_one_generation(gateway: Gateway) -> None:
    """One queued job, tunnel, proof pair and request; nothing sent afterwards."""
    assert [path for method, path, _ in gateway.calls if method == "POST"] == ["/run"]
    assert gateway.connects == gateway.generations == 1
    assert gateway.worker is not None and gateway.worker.closed
    assert [json.loads(message)["type"] for message in gateway.worker.sent] == [
        "handshake",
        "authorize",
    ]


def test_delivered_stream_only_adds_status_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    client, gateway = start(tmp_path, monkeypatch, clock, deliver=True)
    with client:
        result = client.create(stream=True, model="fixture", input="hello")
        response = (client._state / gateway.identifier / "response.bin").read_bytes()

    assert result.response.id == "resp_recorded"
    assert result.usage == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    assert response == BODY
    assert gateway.paths("/progress") == [60, 120, 180, ISSUED_AT]
    assert gateway.paths("/response.sse") == []
    assert gateway.worker is not None
    assert gateway.worker.reads == [600, 600, 60, 60, 60, 60, 60]
    assert_one_generation(gateway)


@pytest.mark.parametrize(
    "stall_timeout,polls",
    [
        (600, [60, 120, 180, 240, 840]),
        (120, [60, 120, 180, 240, 360]),
        (30, [30, 60, 90, 120, 150, 180, 210, 240]),
    ],
)
def test_issued_but_silent_stream_is_closed_and_resumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
    stall_timeout: float,
    polls: list[float],
) -> None:
    client, gateway = start(
        tmp_path, monkeypatch, clock, deliver=False, stall_timeout=stall_timeout
    )
    with client:
        result = client.create(stream=True, model="fixture", input="hello")
        response = (client._state / gateway.identifier / "response.bin").read_bytes()

    # Status reads run only after authorization; the last one is resume().
    # The stream is abandoned stall_timeout after the first issued status.
    interval = min(60, stall_timeout)
    assert gateway.paths("/progress") == polls
    assert gateway.paths("/response.sse") == [polls[-1]]
    assert polls[-1] - polls[-2] == stall_timeout
    assert gateway.worker is not None
    assert gateway.worker.reads == [600, 600] + [interval] * int(polls[-1] / interval)
    assert result.response.id == "resp_recorded"
    assert result.usage == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    assert response == BODY
    assert_one_generation(gateway)


@pytest.mark.parametrize(
    "failure",
    ["unavailable", "transport", "invalid", "unknown_job", "object_not_ready"],
)
@pytest.mark.parametrize("deliver", [True, False])
def test_status_read_failures_never_end_a_live_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
    failure: str,
    deliver: bool,
) -> None:
    client, gateway = start(
        tmp_path, monkeypatch, clock, deliver=deliver, failure=failure
    )
    with client:
        result = client.create(stream=True, model="fixture", input="hello")

    assert result.response.id == "resp_recorded"
    if deliver:
        assert gateway.paths("/progress") == [60, 120, 180, ISSUED_AT]
        assert gateway.paths("/response.sse") == []
    else:
        assert gateway.paths("/progress") == [60, 120, 180, 240, 840]
        assert gateway.paths("/response.sse") == [840]
    assert_one_generation(gateway)


@pytest.mark.parametrize("failure", ["unavailable", "transport", "invalid"])
def test_unreadable_status_keeps_the_original_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float], failure: str
) -> None:
    client, gateway = start(
        tmp_path, monkeypatch, clock, deliver=False, failure=failure, persistent=True
    )
    with client, pytest.raises(WitnessError) as error:
        client.create(stream=True, model="fixture", input="hello")

    assert error.value.code == "connection_lost"
    assert error.value.exchange_id == gateway.identifier
    assert clock[0] == 3600
    assert gateway.paths("/progress") == [60.0 * n for n in range(1, 60)]
    assert gateway.paths("/response.sse") == []
    assert_one_generation(gateway)


def test_failed_recovery_after_a_stall_keeps_the_same_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    client, gateway = start(
        tmp_path, monkeypatch, clock, deliver=False, download_failures=1
    )
    with client:
        with pytest.raises(WitnessError) as error:
            client.create(stream=True, model="fixture", input="hello")
        assert error.value.exchange_id == gateway.identifier
        assert error.value.status_code == 500
        assert error.value.code == "storage_unavailable"
        assert not (client._state / gateway.identifier / "response.bin").exists()
        result = client.resume(gateway.identifier)

    assert result.response.id == "resp_recorded"
    assert gateway.paths("/response.sse") == [840, 840]
    assert_one_generation(gateway)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_stall_timeout_is_rejected_before_creating_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid stall configuration must not open an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    config: WitnessConfig = {
        "witness_url": "https://witness.test",
        "proof_parameters": str(tmp_path / "parameters"),
        "state_directory": str(tmp_path / "state"),
    }
    with pytest.raises(ValueError, match="stall_timeout"):
        WitnessClient(
            config,
            hotkey=Keypair.create_from_mnemonic(Keypair.generate_mnemonic()),
            openai_api_key="x" * 80,
            stall_timeout=timeout,
        )
    assert not (tmp_path / "state").exists()
