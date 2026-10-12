"""The Workers API surface used by this application, for static checking."""

from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol


class JsValue(Protocol):
    def to_py(self) -> object: ...


class Buffer(Protocol):
    byteLength: int

    def to_bytes(self) -> bytes: ...


class Chunk(Protocol):
    done: bool
    value: Buffer


class Reader(Protocol):
    async def read(self) -> Chunk: ...
    async def cancel(self) -> None: ...


class Stream(Protocol):
    def getReader(self) -> Reader: ...
    async def cancel(self) -> None: ...


class Headers(Protocol):
    def get(self, key: str) -> str | None: ...


class HttpRequest(Protocol):
    url: str
    method: str
    headers: Headers
    body: Stream | None


class HttpResponse(Protocol):
    status: int
    ok: bool
    body: Stream | None


class Url(Protocol):
    pathname: str
    search: str
    origin: str
    protocol: str


class Socket(Protocol):
    readyState: int

    def serializeAttachment(self, attachment: object) -> None: ...
    def deserializeAttachment(self) -> JsValue: ...
    def send(self, message: str | Buffer) -> None: ...
    def close(self, code: int, reason: str) -> None: ...


class SocketPair(Protocol):
    def object_values(self) -> tuple[Socket, Socket]: ...


class Nonces(Protocol):
    def entries(self) -> Iterable[tuple[str, int]]: ...


class Storage(Protocol):
    async def get(self, key: str) -> object: ...
    async def put(self, key_or_values: object, value: object = ...) -> None: ...
    async def delete(self, key: str) -> bool: ...
    async def list(self, options: object) -> Nonces: ...
    async def setAlarm(self, time: int) -> None: ...


class DurableState(Protocol):
    storage: Storage

    def waitUntil(self, promise: Awaitable[object]) -> None: ...
    async def blockConcurrencyWhile(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None: ...
    def acceptWebSocket(self, socket: Socket, tags: object) -> None: ...
    def getWebSockets(self, tag: str) -> Iterable[Socket]: ...


class Stub(Protocol):
    async def fetch(self, request: HttpRequest) -> HttpResponse: ...


class Namespace(Protocol):
    def getByName(self, name: str) -> Stub: ...


class KV(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def put(self, key: str, value: str) -> None: ...


class Evidence(Protocol):
    size: int
    body: Stream


class Bucket(Protocol):
    async def get(self, key: str) -> Evidence | None: ...
