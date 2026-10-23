"""Bounded TLS bytes and verification messages on one live WebSocket."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from .delivery import Phase
from .errors import WitnessError
from .types import ExchangeStatus

MAX_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


class Socket(Protocol):
    def send(self, message: str | bytes) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> None: ...


class WebSocketTunnel:
    """Carry one TLS connection; a broken socket requires result recovery or replacement.

    Binary messages carry encrypted TLS bytes. Text messages carry proofs,
    verification acknowledgements, and an explicit end of the recorded stream.
    """

    def __init__(self, socket: Socket, identifier: str) -> None:
        self.socket, self.identifier = socket, identifier
        self.buffer = bytearray()
        self.pending: deque[bytes] = deque()
        self.upload_bytes = self.download_bytes = 0
        self.received_messages = 0
        self.ended = False
        self.idle_check: Callable[[], None] | None = None
        self.idle_interval = 0.0

    def watch(self, check: Callable[[], None], interval: float) -> None:
        """Run ``check`` after every ``interval`` seconds without a message.

        Without a check, a wait is one socket read until its deadline. ``check``
        may raise to abandon the wait; the caller closes the tunnel.
        """
        if not interval > 0:
            raise ValueError("Tunnel check interval must be > 0 seconds")
        self.idle_check, self.idle_interval = check, interval

    def send(self, data: bytes) -> None:
        self.upload_bytes += len(data)
        if self.upload_bytes > MAX_BYTES:
            raise WitnessError("TLS upload exceeds 16 MiB", self.identifier)
        self.buffer.extend(data)
        if len(self.buffer) >= CHUNK_BYTES:
            self.flush()

    def flush(self) -> None:
        while self.buffer:
            self.socket.send(bytes(self.buffer[:CHUNK_BYTES]))
            del self.buffer[:CHUNK_BYTES]

    def recv(self, timeout: float | None = None) -> bytes:
        self.flush()
        if self.pending:
            return self.pending.popleft()
        deadline = time.monotonic() + (timeout if timeout is not None else 600)
        while not self.ended:
            message = self._receive(max(0, deadline - time.monotonic()))
            if isinstance(message, bytes):
                return message
        return b""

    def verify(self, phase: Phase, claim: bytes, timeout: float) -> ExchangeStatus:
        """Send a proof and wait for the worker's verification acknowledgement."""
        if phase not in ("handshake", "authorize"):
            raise WitnessError("Unsupported authorization phase", self.identifier)
        self.flush()
        self.socket.send(
            json.dumps(
                {"type": phase, "claim": json.loads(claim)}, separators=(",", ":")
            )
        )
        deadline = time.monotonic() + timeout
        while not self.ended:
            message = self._receive(max(0, deadline - time.monotonic()))
            if isinstance(message, bytes):
                self.pending.append(message)
            elif (
                message.get("authorized" if phase == "authorize" else "handshook")
                is True
            ):
                return message
        raise WitnessError("Witness closed before proof acceptance", self.identifier)

    def _next_message(self, timeout: float) -> str | bytes:
        if self.idle_check is None:
            return self.socket.recv(timeout=timeout)
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > self.idle_interval:
            try:
                return self.socket.recv(timeout=self.idle_interval)
            except TimeoutError:
                self.idle_check()
        return self.socket.recv(timeout=max(0, remaining))

    def _receive(self, timeout: float) -> bytes | ExchangeStatus:
        message = self._next_message(timeout)
        self.received_messages += 1
        if self.received_messages > 4096:
            raise WitnessError("Too many transport messages", self.identifier)
        if isinstance(message, bytes):
            self.download_bytes += len(message)
            if (
                not message
                or len(message) > CHUNK_BYTES
                or self.download_bytes > MAX_BYTES
            ):
                raise WitnessError("Invalid TLS download size", self.identifier)
            return message
        if len(message.encode()) > 4096:
            raise WitnessError("Invalid witness control message", self.identifier)
        value = json.loads(message)
        if not isinstance(value, dict):
            raise WitnessError("Invalid witness control message", self.identifier)
        if value.get("type") == "end":
            self.ended = True
            return {"status": "issued"}
        if value.get("type") != "status" or not isinstance(value.get("status"), str):
            raise WitnessError("Invalid witness control message", self.identifier)
        result: ExchangeStatus = {"status": value["status"]}
        for name in ("handshook", "authorized"):
            if name in value and type(value[name]) is not bool:
                raise WitnessError("Invalid witness control message", self.identifier)
        if "handshook" in value:
            result["handshook"] = value["handshook"]
        if "authorized" in value:
            result["authorized"] = value["authorized"]
        return result

    def close(self) -> None:
        self.socket.close()
