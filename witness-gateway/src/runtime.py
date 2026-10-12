"""Conversions at the Python/Workers boundary; application records stay in Python."""

from __future__ import annotations

import json as encoding
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from js import Object, Response
from pyodide.ffi import JsProxy, create_proxy, to_js
from workers import import_from_javascript

from bindings import DurableState, HttpResponse, Storage, Stream
from models import Json, Rejection


class Crypto(Protocol):
    def publicKey(self, hotkey: str) -> str: ...
    async def verify(self, message: str, signature: str, key: str) -> bool: ...
    def encodeHotkey(self, key: str) -> str: ...
    def storageKey(self, pallet: str, name: str) -> str: ...


def crypto_bridge() -> Crypto:
    return cast(Crypto, import_from_javascript("crypto.mjs"))


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def safe_integer(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -9_007_199_254_740_991 <= value <= 9_007_199_254_740_991
        and int(value) == value
    )


def dumps(value: object) -> str:
    return encoding.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def invalid_constant(value: str) -> Json:
    raise ValueError("Invalid JSON constant")


def loads(value: str | bytes) -> Json:
    return cast(Json, encoding.loads(value, parse_constant=invalid_constant))


def js_object(value: object) -> JsProxy:
    return cast(JsProxy, to_js(value, dict_converter=Object.fromEntries))


def json(value: object, status: int = 200) -> HttpResponse:
    return Response.new(
        dumps(value),
        js_object(
            {
                "status": status,
                "headers": {
                    "content-type": "application/json",
                    "cache-control": "no-store",
                },
            }
        ),
    )


def failure(error: Exception) -> HttpResponse:
    if isinstance(error, Rejection):
        body = {"error": str(error)}
        if error.exchange_id:
            body["exchange_id"] = error.exchange_id
        return json(body, error.status)
    return json({"error": "gateway_unavailable"}, 503)


async def bounded_bytes(body: Stream | None, limit: int) -> bytes:
    if not body:
        return b""
    reader = body.getReader()
    result = bytearray()
    try:
        while True:
            chunk = await reader.read()
            if chunk.done:
                break
            if len(result) + chunk.value.byteLength > limit:
                raise Rejection(413, "body_too_large")
            result.extend(chunk.value.to_bytes())
    finally:
        await reader.cancel()
    return bytes(result)


async def bounded_text(body: Stream | None, limit: int) -> str:
    return (await bounded_bytes(body, limit)).decode("utf-8", errors="strict")


async def stored(storage: Storage, key: str) -> Json:
    value = await storage.get(key)
    return cast(Json, value.to_py() if isinstance(value, JsProxy) else value)


async def with_state_lock[T](
    state: DurableState, operation: Callable[[], Awaitable[T]]
) -> T:
    """Keep expected rejections outside the gate so valid sockets stay connected."""
    result: T | None = None
    error: Exception | None = None

    async def protected() -> None:
        nonlocal result, error
        try:
            result = await operation()
        except Exception as caught:
            error = caught

    callback = create_proxy(protected)
    try:
        await state.blockConcurrencyWhile(callback)
    finally:
        callback.destroy()
    if error is not None:
        raise error
    return cast(T, result)
