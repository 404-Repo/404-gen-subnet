"""TLS 1.3 over the witness tunnel, with credential-only key isolation.

tlslite-ng owns TLS and authenticates CertificateVerify/Finished. cryptography
validates the certificate chain before any HTTP bytes are sent. The small hooks
below observe record keys in the supported tlslite 0.8 series, never traffic secrets.
"""

from __future__ import annotations

import ssl
import time
import warnings
from collections.abc import Callable, Generator, Sequence
from os import PathLike
from typing import TYPE_CHECKING, Literal, cast

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.verification import PolicyBuilder, Store

from .tunnel import WebSocketTunnel
from .types import (
    Disclosures,
    EarlyDisclosure,
    EncryptedRecord,
    HandshakeDisclosure,
    PlainRecord,
    PrivateRecord,
    RecordDisclosure,
)

if TYPE_CHECKING:
    from ._tls import (
        Buffer,
        CertificateChain,
        Cipher,
        ConnectionState,
        DecryptedRecord,
        HandshakeSettings,
        Message,
        RecordHeader3,
        Session,
        TLSAbruptCloseError,
        TLSConnection,
        Wait,
        WireRecord,
    )
else:
    from tlslite import HandshakeSettings, TLSConnection
    from tlslite.errors import TLSAbruptCloseError
    from tlslite.messages import Message, RecordHeader3
    from tlslite.recordlayer import ConnectionState

MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 65536


class TunnelSocket:
    """Bounded synchronous socket interface for TLS over a live WebSocket."""

    def __init__(self, tunnel: WebSocketTunnel, timeout: float) -> None:
        self.tunnel = tunnel
        self.deadline = time.monotonic() + timeout
        self.pending = bytearray()
        self.received_bytes = self.sent_bytes = 0

    def send(self, data: bytes | bytearray) -> int:
        self.sent_bytes += len(data)
        if self.sent_bytes > MAX_BYTES or time.monotonic() >= self.deadline:
            raise ValueError("TLS upload limit/deadline exceeded")
        for offset in range(0, len(data), 65536):
            self.tunnel.send(bytes(data[offset : offset + 65536]))
        return len(data)

    def sendall(self, data: bytes | bytearray) -> None:
        self.send(data)

    def recv(self, count: int) -> bytes:
        while not self.pending:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TLS exchange deadline exceeded")
            # The witness releases response bytes after saving evidence. A healthy
            # generation may be silent until then, so wait until the deadline.
            data = self.tunnel.recv(timeout=remaining)
            if not data:
                return b""
            self.received_bytes += len(data)
            if self.received_bytes > MAX_BYTES:
                raise ValueError("TLS response exceeds limit")
            self.pending.extend(data)
        data = bytes(self.pending[:count])
        del self.pending[:count]
        return data

    def close(self) -> None:
        self.tunnel.close()


class Capture:
    def __init__(self, connection: TLSConnection) -> None:
        self.disclosures: Disclosures = {"client": [], "server": []}
        self.private: list[PrivateRecord] = []
        self.hidden = False
        layer = connection._recordLayer
        record_socket = layer._recordSocket
        original_send = record_socket.send
        original_recv = record_socket.recv
        original_send_record = layer.sendRecord

        def send_record(message: Message) -> Generator[Wait, None, None]:
            _install_native_cipher(layer._writeState)
            yield from original_send_record(message)

        def send(message: Message, padding: int = 0) -> Generator[Wait, None, None]:
            info = _record_disclosure(
                layer._writeState, message.contentType == 23, sent=True
            )
            self.append("client", {} if self.hidden else info)
            if self.hidden:
                private_info = cast(EncryptedRecord, info)
                data = message.write()
                header = RecordHeader3().create(
                    record_socket.version, message.contentType, len(data)
                )
                self.private.append(
                    {
                        "key": private_info["key"],
                        "nonce": private_info["iv"],
                        "number": private_info["number"],
                        "record": bytes(header.write() + data).hex(),
                    }
                )
            yield from original_send(message, padding)

        def receive() -> Generator[Wait | WireRecord, None, None]:
            for result in original_recv():
                if result not in (0, 1):
                    header, _ = cast("WireRecord", result)
                    self.append(
                        "server",
                        _record_disclosure(layer._readState, header.type == 23),
                    )
                    _install_native_cipher(layer._readState)
                yield result

        layer.sendRecord = send_record
        record_socket.send, record_socket.recv = send, receive

    def append(self, side: Literal["client", "server"], info: RecordDisclosure) -> None:
        if len(self.disclosures[side]) >= MAX_RECORDS:
            raise ValueError("TLS record count exceeds limit")
        self.disclosures[side].append(info)


def exchange(
    tunnel: WebSocketTunnel,
    host: str,
    chunks: Sequence[tuple[bool, bytes]],
    *,
    handshake: Callable[[HandshakeDisclosure], None],
    authorize: Callable[[EarlyDisclosure], None],
    ca_file: str | PathLike[str] | None = None,
    timeout: float = 30 * 60,
) -> bytes:
    """Send one witnessed TLS request and return the raw HTTP response bytes."""
    socket = TunnelSocket(tunnel, timeout)
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
    connection.handshakeClientCert(
        settings=settings, serverName=host, alpn=[bytearray(b"http/1.1")]
    )
    _check_certificates(connection, host, ca_file)
    # Only this fixed unauthenticated HEAD may precede proof verification. Its
    # keep-alive response avoids the provider's short initial request deadline.
    path = chunks[0][1].split(b" ", 2)[1]
    connection.write(
        b"HEAD "
        + path
        + b" HTTP/1.1\r\nHost: "
        + host.encode()
        + b"\r\nConnection: keep-alive\r\n\r\n"
    )
    response_key = cast(
        EncryptedRecord, _record_disclosure(connection._recordLayer._readState, True)
    )
    tunnel.flush()
    handshake(
        {
            "disclosures": capture.disclosures,
            "response_key": response_key,
            "client_bytes": socket.sent_bytes,
            "probe": True,
        }
    )
    _read_probe(connection)
    for hidden, data in chunks:
        if hidden != capture.hidden:
            # The update is protected by the OLD key, including the private
            # update immediately after the credential. Never disclose that key.
            for _ in connection.send_keyupdate_request(0):
                pass
        capture.hidden = hidden
        connection.write(data)

    # TLS is established, but HTTP is held until the credential proof passes.
    tunnel.flush()
    authorize(
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
    # Read records directly after the authenticated handshake. This protocol
    # permits tickets followed by HTTP and a close alert, but no more key updates.
    while True:
        try:
            for record in connection._recordLayer.recvRecord():
                if record not in (0, 1):
                    break
        except TLSAbruptCloseError:
            break  # HTTP parser must still prove an explicitly complete body.
        header, parser = cast("DecryptedRecord", record)
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
            # The witness independently checks these are NewSessionTickets.
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


def _install_native_cipher(state: ConnectionState) -> None:
    """Use cryptography's native AEAD inside tlslite's existing record layer."""
    cipher = state.encContext
    if cipher is None or getattr(cipher, "_witness_native", False):
        return
    if cipher.name != "chacha20-poly1305":
        raise ValueError("Witness requires ChaCha20-Poly1305")
    aead = ChaCha20Poly1305(bytes(cipher.key))

    def seal(
        nonce: bytes | bytearray, data: bytes | bytearray, aad: bytes | bytearray
    ) -> bytearray:
        return bytearray(aead.encrypt(bytes(nonce), bytes(data), bytes(aad)))

    def open_record(
        nonce: bytes | bytearray, data: bytes | bytearray, aad: bytes | bytearray
    ) -> bytearray | None:
        try:
            return bytearray(aead.decrypt(bytes(nonce), bytes(data), bytes(aad)))
        except InvalidTag:
            return None  # tlslite raises TLSBadRecordMAC

    cipher.seal, cipher.open, cipher._witness_native = seal, open_record, True


def _record_disclosure(
    state: ConnectionState, encrypted: bool, sent: bool = False
) -> PlainRecord | EncryptedRecord:
    if not encrypted:
        return {"plain": True}
    # An encrypted record has a cipher and nonce installed by the TLS library.
    return {
        "key": bytes(cast("Cipher", state.encContext).key).hex(),
        "iv": bytes(cast("Buffer", state.fixedNonce)).hex(),
        "number": state.seqnum - int(sent),
    }


def _check_certificates(
    connection: TLSConnection, host: str, ca_file: str | PathLike[str] | None
) -> None:
    session = cast("Session", connection.session)
    certificates = cast("CertificateChain", session.serverCertChain).x509List
    if not 1 <= len(certificates) <= 16:
        raise ValueError("Invalid provider certificate chain length")
    chain = [x509.load_der_x509_certificate(bytes(c.bytes)) for c in certificates]
    context = ssl.create_default_context(cafile=ca_file)
    # System trust stores still include legacy roots with serial number zero.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parsed a serial number which wasn't positive",
            category=CryptographyDeprecationWarning,
        )
        roots = [
            x509.load_der_x509_certificate(c)
            for c in context.get_ca_certs(binary_form=True)
        ]
    verifier = (
        PolicyBuilder().store(Store(roots)).build_server_verifier(x509.DNSName(host))
    )
    verifier.verify(chain[0], chain[1:])


def _read_probe(connection: TLSConnection) -> None:
    """Read only the fixed HEAD response before proof preparation."""
    response = bytearray()
    while True:
        for record in connection._recordLayer.recvRecord():
            if record not in (0, 1):
                break
        header, parser = cast("DecryptedRecord", record)
        if header.type == 22 and not response:
            continue  # The witness independently checks session ticket contents.
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
