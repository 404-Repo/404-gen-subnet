"""Types for the tlslite-ng 0.8.2 interfaces used by our TLS 1.3 transport.

The dependency has no type stubs. These declarations describe its existing
objects; they introduce no runtime wrapper.
"""

from collections.abc import Callable, Generator
from typing import Literal, Protocol, Self, TypeAlias, overload

Buffer: TypeAlias = bytes | bytearray
Wait: TypeAlias = Literal[0, 1]

class Socket(Protocol):
    def send(self, data: Buffer) -> int: ...
    def sendall(self, data: Buffer) -> None: ...
    def recv(self, count: int) -> bytes: ...
    def close(self) -> None: ...

class Cipher(Protocol):
    name: str
    key: Buffer
    _witness_native: bool
    seal: Callable[[Buffer, Buffer, Buffer], bytearray]
    open: Callable[[Buffer, Buffer, Buffer], bytearray | None]

class ConnectionState:
    encContext: Cipher | None
    fixedNonce: Buffer | None
    seqnum: int

class Message(Protocol):
    contentType: int
    def write(self) -> bytearray: ...

class RecordHeader3:
    type: int
    def create(self, version: tuple[int, int], type: int, length: int) -> Self: ...
    def write(self) -> bytearray: ...

class Parser:
    bytes: bytearray

WireRecord: TypeAlias = tuple[RecordHeader3, bytearray]
DecryptedRecord: TypeAlias = tuple[RecordHeader3, Parser]

class RecordSocket:
    version: tuple[int, int]
    send: Callable[[Message, int], Generator[Wait]]
    recv: Callable[[], Generator[Wait | WireRecord]]

class RecordLayer:
    _recordSocket: RecordSocket
    _writeState: ConnectionState
    _readState: ConnectionState
    sendRecord: Callable[[Message], Generator[Wait]]
    def recvRecord(self) -> Generator[Wait | DecryptedRecord]: ...

class Certificate:
    bytes: bytearray

class CertificateChain:
    x509List: list[Certificate]

class Session:
    serverCertChain: CertificateChain | None

class HandshakeSettings:
    minVersion: tuple[int, int]
    maxVersion: tuple[int, int]
    versions: list[tuple[int, int]]
    cipherNames: list[str]
    keyShares: list[str]
    eccCurves: list[str]
    dhGroups: list[str]
    certificate_compression_receive: list[str]
    use_heartbeat_extension: bool

class TLSConnection:
    _recordLayer: RecordLayer
    session: Session | None
    def __init__(self, sock: Socket) -> None: ...
    @overload
    def handshakeClientCert(
        self, *, settings: HandshakeSettings, serverName: str, alpn: list[bytearray]
    ) -> None: ...
    @overload
    def handshakeClientCert(
        self, *, settings: HandshakeSettings, serverName: str, alpn: list[bytearray],
        async_: Literal[True],
    ) -> Generator[Wait]: ...
    def writeAsync(self, data: bytes) -> Generator[Wait]: ...
    def send_keyupdate_request(self, message_type: int) -> Generator[Wait]: ...
    def write(self, data: bytes) -> None: ...

class TLSAbruptCloseError(Exception): ...
