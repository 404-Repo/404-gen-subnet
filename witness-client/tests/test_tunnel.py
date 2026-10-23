"""Live TLS framing keeps proofs ordered and never retries a broken exchange."""

import json
from collections import deque
from typing import TYPE_CHECKING, cast

import pytest

from witness_client import WitnessError
from witness_client.delivery import Phase
from witness_client.transport import TunnelSocket
from witness_client.tunnel import CHUNK_BYTES, MAX_BYTES, WebSocketTunnel

if TYPE_CHECKING:
    from witness_client._tls import TLSConnection
else:
    from tlslite import TLSConnection


class Socket:
    def __init__(self, incoming: list[str | bytes]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[str | bytes] = []

    def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str | bytes:
        if not self.incoming:
            raise ConnectionError("Lost connection")
        return self.incoming.popleft()

    def close(self) -> None:
        pass


class QuietSocket(Socket):
    """Return scripted messages; None waits out the whole read timeout."""

    def __init__(self, clock: list[float], script: list[str | bytes | None]) -> None:
        super().__init__([])
        self.clock, self.script = clock, deque(script)
        self.timeouts: list[float | None] = []

    def recv(self, timeout: float | None = None) -> str | bytes:
        self.timeouts.append(timeout)
        message = self.script.popleft() if self.script else None
        if message is None:
            assert timeout is not None
            self.clock[0] += timeout
            raise TimeoutError
        return message


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr("witness_client.tunnel.time.monotonic", lambda: now[0])
    return now


def test_proof_wait_keeps_tls_bytes_and_flushes_request_first() -> None:
    socket = Socket(
        [
            b"provider TLS",
            '{"type":"status","status":"running","authorized":true}',
            '{"type":"end"}',
        ]
    )
    tunnel = WebSocketTunnel(socket, "fixture")
    tunnel.send(b"request TLS")
    assert tunnel.verify("authorize", b'{"proof":"fixture"}', 1)["authorized"]
    assert socket.sent[0] == b"request TLS"
    assert json.loads(str(socket.sent[1])) == {
        "type": "authorize",
        "claim": {"proof": "fixture"},
    }
    assert tunnel.recv(1) == b"provider TLS"
    assert tunnel.recv(1) == b""


def test_lost_proof_acknowledgement_does_not_repeat_proof_or_generation() -> None:
    socket = Socket([])
    tunnel = WebSocketTunnel(socket, "fixture")
    with pytest.raises(ConnectionError):
        tunnel.verify("authorize", b"{}", 1)
    assert len(socket.sent) == 1


def test_invalid_phase_is_rejected_before_sending_tls_or_proofs() -> None:
    socket = Socket([])
    tunnel = WebSocketTunnel(socket, "fixture")
    tunnel.send(b"request TLS")
    with pytest.raises(WitnessError, match="Unsupported authorization phase") as error:
        tunnel.verify(cast(Phase, "invalid"), b"{}", 1)
    assert error.value.exchange_id == "fixture"
    assert error.value.source == "client"
    assert socket.sent == []


@pytest.mark.parametrize(
    "message",
    [
        b"",
        b"x" * (CHUNK_BYTES + 1),
        "[]",
        '{"type":"status","status":true}',
        '{"type":"status","status":"running","authorized":"true"}',
        '{"type":"authorize"}',
    ],
)
def test_invalid_worker_messages_are_rejected(message: str | bytes) -> None:
    with pytest.raises(WitnessError):
        WebSocketTunnel(Socket([message]), "fixture").recv(1)


def test_limits_cover_the_entire_stream() -> None:
    socket = Socket([b"x" * CHUNK_BYTES] * 65)
    tunnel = WebSocketTunnel(socket, "fixture")
    for _ in range(64):
        assert len(tunnel.recv(1)) == CHUNK_BYTES
    with pytest.raises(WitnessError, match="size"):
        tunnel.recv(1)
    with pytest.raises(WitnessError, match="upload"):
        tunnel.send(b"x" * (MAX_BYTES + 1))


def test_unwatched_wait_is_one_read_until_the_deadline(clock: list[float]) -> None:
    socket = QuietSocket(clock, [])
    with pytest.raises(TimeoutError):
        WebSocketTunnel(socket, "fixture").recv(600)
    assert socket.timeouts == [600]


def test_watched_wait_checks_after_each_silent_interval(clock: list[float]) -> None:
    checks: list[float] = []
    socket = QuietSocket(clock, [None, None, b"provider TLS", None, '{"type":"end"}'])
    tunnel = WebSocketTunnel(socket, "fixture")
    tunnel.watch(lambda: checks.append(clock[0]), 60)
    assert tunnel.recv(600) == b"provider TLS"
    assert tunnel.recv(600) == b""
    assert socket.timeouts == [60, 60, 60, 60, 60]
    assert checks == [60, 120, 180]


def test_watched_wait_keeps_the_original_deadline(clock: list[float]) -> None:
    checks: list[float] = []
    socket = QuietSocket(clock, [])
    tunnel = WebSocketTunnel(socket, "fixture")
    tunnel.watch(lambda: checks.append(clock[0]), 60)
    with pytest.raises(TimeoutError):
        tunnel.recv(150)
    assert socket.timeouts == [60, 60, 30]
    assert checks == [60, 120]
    assert clock[0] == 150


def test_failed_check_ends_the_wait_across_the_tls_layer(clock: list[float]) -> None:
    socket = QuietSocket(clock, [])
    tunnel = WebSocketTunnel(socket, "fixture")
    stalled = WitnessError("Stalled stream", "fixture")

    def check() -> None:
        raise stalled

    tunnel.watch(check, 60)
    connection = TLSConnection(TunnelSocket(tunnel, 600))
    with pytest.raises(WitnessError) as error:
        for _ in connection._recordLayer.recvRecord():
            pass
    assert error.value is stalled
    assert socket.timeouts == [60]
    assert socket.sent == []


@pytest.mark.parametrize("interval", [0, -1, float("nan")])
def test_watch_requires_a_positive_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="interval"):
        WebSocketTunnel(Socket([]), "fixture").watch(lambda: None, interval)
