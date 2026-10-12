from collections.abc import Awaitable, Iterable
from typing import Protocol
from bindings import HttpRequest, HttpResponse, SocketPair, Url
from pyodide.ffi import JsArray, JsProxy

class Object:
    @staticmethod
    def fromEntries(entries: Iterable[JsArray[object]]) -> JsProxy: ...

class URL:
    @staticmethod
    def new(url: str) -> Url: ...

class Request(HttpRequest, Protocol):
    @staticmethod
    def new(url: str, options: object) -> HttpRequest: ...

class Response(HttpResponse, Protocol):
    @staticmethod
    def new(body: object, options: object) -> HttpResponse: ...

class WebSocketPair:
    @staticmethod
    def new() -> SocketPair: ...

class AbortSignal:
    @staticmethod
    def timeout(milliseconds: int) -> object: ...

def fetch(url: str, options: object) -> Awaitable[HttpResponse]: ...
