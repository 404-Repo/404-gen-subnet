"""Awaitable PNG rendering through the existing JavaScript HTTP service."""

from __future__ import annotations

import math
from types import TracebackType
from typing import Protocol, Self

import httpx

from .client import (
    DEFAULT_OPTIONS,
    MAX_ERROR_BYTES,
    MAX_RESPONSE_BYTES,
    PNG_SIGNATURE,
    RenderOptions,
    RenderRejected,
    RenderRequest,
    RenderUnavailable,
)


class AsyncRenderer(Protocol):
    @property
    def renderer_id(self) -> str: ...

    async def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes: ...


class AsyncRendererClient:
    """Call an already-running JS renderer. No container management or automatic retries."""

    def __init__(
        self,
        url: str,
        *,
        renderer_id: str,
        api_key: str | None = None,
        timeout: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        endpoint = httpx.URL(url)
        if endpoint.scheme not in ("http", "https") or not endpoint.host or endpoint.userinfo:
            raise ValueError("Renderer URL must be HTTP(S) without embedded credentials")
        if endpoint.query or endpoint.fragment:
            raise ValueError("Renderer URL cannot contain a query or fragment")
        if not renderer_id:
            raise ValueError("renderer_id must identify the pinned renderer image")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.renderer_id = renderer_id
        self._url = str(endpoint).rstrip("/") + "/render/grid"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            headers=headers, timeout=timeout, transport=transport, trust_env=False, follow_redirects=False
        )

    async def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        RenderRequest.from_source(self.renderer_id, source, options)
        try:
            async with self._http.stream(
                "POST", self._url, params=options.query(), json={"source": source}
            ) as response:
                return await _render_response(response)
        except httpx.HTTPError as error:
            raise RenderUnavailable(f"Renderer request failed: {error}") from error

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        if len(body) + len(chunk) > limit:
            raise RenderUnavailable("Renderer response exceeds the size limit")
        body.extend(chunk)
    return bytes(body)


async def _render_response(response: httpx.Response) -> bytes:
    if response.status_code == 422:
        body = await _read_bounded(response, MAX_ERROR_BYTES)
        try:
            rejection = RenderRejected(body.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise RenderUnavailable("Invalid renderer rejection response") from error
        raise rejection
    response.raise_for_status()
    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "image/png":
        raise RenderUnavailable("Renderer did not return image/png")
    png = await _read_bounded(response, MAX_RESPONSE_BYTES)
    if not png.startswith(PNG_SIGNATURE):
        raise RenderUnavailable("Renderer returned an invalid PNG signature")
    return png
