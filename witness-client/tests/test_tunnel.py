"""Live TLS framing keeps proofs ordered and never retries a broken exchange."""

import json
from collections import deque
from typing import cast

import pytest

from witness_client import WitnessError
from witness_client.delivery import Phase
from witness_client.tunnel import CHUNK_BYTES, MAX_BYTES, WebSocketTunnel


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
