"""Bounded TLS bytes and verification messages on one live WebSocket."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Protocol

from .delivery import Phase
from .errors import WitnessError
from .timing import transport
from .types import ExchangeStatus

MAX_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


class AsyncSocket(Protocol):
    async def send(self, message: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class AsyncWebSocketTunnel:
    """Carry one TLS connection; a broken socket requires result recovery or replacement.

    Binary messages carry encrypted TLS bytes. Text messages carry proofs,
    verification acknowledgements, and an explicit end of the recorded stream.
    """

    def __init__(self, socket: AsyncSocket, identifier: str) -> None:
        self.socket, self.identifier = socket, identifier
        self.buffer = bytearray()
        self.pending: deque[bytes] = deque()
        self.upload_bytes = self.download_bytes = 0
        self.received_messages = 0
        self.sent_messages = 0
        self.ended = False

    async def send(self, data: bytes) -> None:
        self.upload_bytes += len(data)
        if self.upload_bytes > MAX_BYTES:
            raise WitnessError("TLS upload exceeds 16 MiB", self.identifier)
        self.buffer.extend(data)
        if len(self.buffer) >= CHUNK_BYTES:
            await self.flush()

    async def flush(self) -> None:
        while self.buffer:
            await self._send(bytes(self.buffer[:CHUNK_BYTES]), "binary")
            del self.buffer[:CHUNK_BYTES]

    async def _send(self, message: str | bytes, kind: str) -> None:
        self.sent_messages += 1
        transport(
            "send_start", self.identifier, kind=kind, size=len(message), sequence=self.sent_messages
        )
        await self.socket.send(message)
        transport(
            "send_end", self.identifier, kind=kind, size=len(message), sequence=self.sent_messages
        )

    async def recv(self, timeout: float | None = None) -> bytes:
        await self.flush()
        if self.pending:
            return self.pending.popleft()
        deadline = time.monotonic() + (timeout if timeout is not None else 600)
        while not self.ended:
            message = await self._receive(max(0, deadline - time.monotonic()))
            if isinstance(message, bytes):
                return message
        return b""

    async def verify(
        self,
        phase: Phase,
        claim: bytes,
        timeout: float,
        *,
        deadline: float | None = None,
    ) -> ExchangeStatus:
        """Send a proof and wait for the worker's verification acknowledgement."""
        if phase not in ("handshake", "authorize"):
            raise WitnessError("Unsupported authorization phase", self.identifier)
        deadline = (
            min(deadline, time.monotonic() + timeout)
            if deadline is not None
            else time.monotonic() + timeout
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("Proof verification deadline reached")
        await self.flush()
        proof = json.dumps({"type": phase, "claim": json.loads(claim)}, separators=(",", ":"))
        if time.monotonic() >= deadline:
            raise TimeoutError("Proof verification deadline reached")
        await self._send(proof, phase)
        while not self.ended:
            message = await self._receive(max(0, deadline - time.monotonic()))
            if isinstance(message, bytes):
                self.pending.append(message)
            elif message.get("authorized" if phase == "authorize" else "handshook") is True:
                return message
        raise WitnessError("Witness closed before proof acceptance", self.identifier)

    async def _receive(self, timeout: float) -> bytes | ExchangeStatus:
        try:
            async with asyncio.timeout(timeout):
                message = await self.socket.recv()
        except Exception as error:
            transport("receive_failed", self.identifier, error_type=type(error).__name__)
            raise
        self.received_messages += 1
        transport(
            "receive",
            self.identifier,
            kind="binary" if isinstance(message, bytes) else "control",
            size=len(message),
            sequence=self.received_messages,
        )
        if self.received_messages > 4096:
            raise WitnessError("Too many transport messages", self.identifier)
        if isinstance(message, bytes):
            self.download_bytes += len(message)
            if not message or len(message) > CHUNK_BYTES or self.download_bytes > MAX_BYTES:
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

    async def close(self) -> None:
        await self.socket.close()
