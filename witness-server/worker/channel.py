"""Live connection from a queued RunPod worker to the gateway."""

from __future__ import annotations

import json
import time
from typing import Protocol

from websockets.sync.connection import Connection

from storage import CHUNK_BYTES, MAX_BYTES, json_bytes


class Channel(Protocol):
    def recv(self, deadline: float) -> bytes: ...
    def claim(self, phase: str) -> bytes | None: ...
    def send(self, data: bytes) -> None: ...
    def notify(self, status: dict[str, object]) -> None: ...
    def finish(self) -> None: ...


class WorkerChannel:
    """TLS bytes and two verification claims on a single assigned connection."""

    def __init__(self, socket: Connection) -> None:
        self.socket = socket
        self.claims: dict[str, bytes] = {}
        self.received_bytes = self.received_messages = 0
        self.sent_bytes = 0

    def recv(self, deadline: float) -> bytes:
        message = self.socket.recv(timeout=max(0, deadline - time.monotonic()))
        self.received_messages += 1
        if self.received_messages > 4096:
            raise ValueError("Too many transport messages")
        if isinstance(message, bytes):
            self.received_bytes += len(message)
            if (
                not message
                or len(message) > CHUNK_BYTES
                or self.received_bytes > MAX_BYTES
            ):
                raise ValueError("Invalid client traffic size")
            return message
        if len(message.encode()) > 2 * 1024 * 1024:
            raise ValueError("Proof exceeds limit")
        value = json.loads(message)
        if not isinstance(value, dict) or value.get("type") not in (
            "handshake",
            "authorize",
        ):
            raise ValueError("Invalid miner message")
        phase = value["type"]
        if phase in self.claims or not isinstance(value.get("claim"), dict):
            raise ValueError("Invalid or repeated proof")
        self.claims[phase] = json_bytes(value["claim"])
        raise TimeoutError("Control message received")

    def claim(self, phase: str) -> bytes | None:
        return self.claims.get(phase)

    def send(self, data: bytes) -> None:
        self.sent_bytes += len(data)
        if self.sent_bytes > MAX_BYTES:
            raise ValueError("Provider traffic exceeds limit")
        for offset in range(0, len(data), CHUNK_BYTES):
            self.socket.send(data[offset : offset + CHUNK_BYTES])

    def notify(self, status: dict[str, object]) -> None:
        self.socket.send(json_bytes({"type": "status", **status}).decode())

    def finish(self) -> None:
        self.socket.send('{"type":"end"}')
        self.socket.send('{"type":"finished"}')
