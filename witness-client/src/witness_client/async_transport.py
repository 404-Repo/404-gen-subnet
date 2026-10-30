"""Drive tlslite's nonblocking TLS generators over a native async WebSocket."""

from __future__ import annotations

import asyncio
import errno
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from os import PathLike
from typing import TYPE_CHECKING, cast

from .async_tunnel import AsyncWebSocketTunnel
from .timing import measure
from .transport import MAX_BYTES, Capture, _check_certificates, _record_disclosure
from .types import EarlyDisclosure, EncryptedRecord, HandshakeDisclosure

if TYPE_CHECKING:
    from ._tls import DecryptedRecord, HandshakeSettings, TLSAbruptCloseError, TLSConnection
else:
    from tlslite import HandshakeSettings, TLSConnection
    from tlslite.errors import TLSAbruptCloseError


class _TLSSocket:
    """In-memory nonblocking socket; only the driver performs network I/O."""

    def __init__(self, tunnel: AsyncWebSocketTunnel, timeout: float) -> None:
        self.tunnel = tunnel
        self.deadline = time.monotonic() + timeout
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.sent_bytes = self.received_bytes = 0
        self.ended = False

    def send(self, data: bytes | bytearray) -> int:
        self.sent_bytes += len(data)
        if self.sent_bytes > MAX_BYTES or time.monotonic() >= self.deadline:
            raise ValueError("TLS upload limit/deadline exceeded")
        self.outgoing.extend(data)
        return len(data)

    def sendall(self, data: bytes | bytearray) -> None:
        self.send(data)

    def recv(self, count: int) -> bytes:
        if not self.incoming and not self.ended:
            raise BlockingIOError(errno.EWOULDBLOCK, "TLS needs encrypted input")
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def close(self) -> None:
        self.ended = True

    async def _send(self) -> None:
        if self.outgoing:
            await self.tunnel.send(bytes(self.outgoing))
            self.outgoing.clear()

    async def run(self, operation: Generator[object, None, None]) -> object:
        try:
            for result in operation:
                await self._send()
                if result == 0:
                    data = await self.tunnel.recv(self.deadline - time.monotonic())
                    self.received_bytes += len(data)
                    if self.received_bytes > MAX_BYTES:
                        raise ValueError("TLS response exceeds limit")
                    self.incoming.extend(data)
                    self.ended = not data
                elif result == 1:
                    await self.tunnel.flush()
                else:
                    return result
            await self._send()
            return None
        finally:
            operation.close()


async def async_exchange(
    tunnel: AsyncWebSocketTunnel,
    host: str,
    chunks: Sequence[tuple[bool, bytes]],
    *,
    handshake: Callable[[HandshakeDisclosure], Awaitable[None]],
    authorize: Callable[[EarlyDisclosure], Awaitable[None]],
    ca_file: str | PathLike[str] | None = None,
    timeout: float = 30 * 60,
) -> bytes:
    """Capture TLS with native async I/O and the synchronous disclosure format."""
    socket = _TLSSocket(tunnel, timeout)
    connection = TLSConnection(socket)
    capture = Capture(connection)
    settings = HandshakeSettings()
    settings.minVersion = settings.maxVersion = (3, 4)
    settings.versions = [(3, 4)]
    settings.cipherNames = ["chacha20-poly1305"]
    settings.keyShares = ["x25519", "secp256r1"]
    settings.eccCurves = ["x25519", "secp256r1"]
    settings.dhGroups = []
    settings.certificate_compression_receive = []
    settings.use_heartbeat_extension = False
    async with asyncio.timeout(timeout):
        with measure("tls_handshake"):
            await socket.run(
                connection.handshakeClientCert(
                    settings=settings,
                    serverName=host,
                    alpn=[bytearray(b"http/1.1")],
                    async_=True,
                )
            )
        with measure("certificate_validation"):
            _check_certificates(connection, host, ca_file)
        path = chunks[0][1].split(b" ", 2)[1]
        await socket.run(
            connection.writeAsync(
                b"HEAD "
                + path
                + b" HTTP/1.1\r\nHost: "
                + host.encode()
                + b"\r\nConnection: keep-alive\r\n\r\n"
            )
        )
        response_key = cast(
            EncryptedRecord, _record_disclosure(connection._recordLayer._readState, True)
        )
        await tunnel.flush()
        await handshake(
            {
                "disclosures": capture.disclosures,
                "response_key": response_key,
                "client_bytes": socket.sent_bytes,
                "probe": True,
            }
        )
        with measure("probe_response_wait"):
            await _read_probe(connection, socket)
        with measure("request_tls_encoding"):
            for hidden, data in chunks:
                if hidden != capture.hidden:
                    await socket.run(connection.send_keyupdate_request(0))
                capture.hidden = hidden
                await socket.run(connection.writeAsync(data))
        await tunnel.flush()
        await authorize(
            {
                "disclosures": capture.disclosures,
                "private": capture.private,
                "response_key": response_key,
                "client_bytes": socket.sent_bytes,
                "probe": True,
            }
        )
        capture.private.clear()
        response = bytearray()
        closed = False
        with measure("response_delivery_wait_and_decrypt"):
            while True:
                try:
                    header, parser = cast(
                        "DecryptedRecord", await socket.run(connection._recordLayer.recvRecord())
                    )
                except TLSAbruptCloseError:
                    break
                data = bytes(parser.bytes)
                if closed:
                    raise ValueError("TLS data after close")
                if header.type == 23:
                    response.extend(data)
                    if len(response) > MAX_BYTES:
                        raise ValueError("HTTP response exceeds limit")
                elif header.type == 21 and data == b"\x01\x00":
                    closed = True
                elif header.type == 22 and not response:
                    offset = 0
                    while offset < len(data):
                        if offset + 4 > len(data) or data[offset] != 4:
                            raise ValueError("Unsupported post-handshake message")
                        offset += 4 + int.from_bytes(data[offset + 1 : offset + 4], "big")
                    if offset != len(data):
                        raise ValueError("Truncated session ticket")
                else:
                    raise ValueError("Unsupported provider TLS record")
        return bytes(response)


async def _read_probe(connection: TLSConnection, socket: _TLSSocket) -> None:
    response = bytearray()
    while True:
        header, parser = cast(
            "DecryptedRecord", await socket.run(connection._recordLayer.recvRecord())
        )
        if header.type == 22 and not response:
            continue
        if header.type != 23:
            raise ValueError("Provider closed the HEAD probe")
        response.extend(parser.bytes)
        if len(response) > 32768:
            raise ValueError("HEAD response headers exceed limit")
        end = response.find(b"\r\n\r\n")
        if end >= 0:
            if end + 4 != len(response):
                raise ValueError("Unexpected body after HEAD probe")
            return
