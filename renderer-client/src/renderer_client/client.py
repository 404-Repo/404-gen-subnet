"""Synchronous PNG rendering through the existing JavaScript HTTP service."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, cast

import httpx

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SOURCE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 256 * 1024


class RendererError(Exception):
    """Base class for rendering and replay errors."""


class RenderUnavailable(RendererError):
    """Transport, service, authentication or response-protocol failure."""


class RenderRejected(RendererError):
    """The renderer returned a structured HTTP 422 rejection of the input."""

    def __init__(self, detail_json: str) -> None:
        details = json.loads(detail_json)
        if not isinstance(details, dict) or not isinstance(details.get("error"), str):
            raise ValueError("Expected a renderer error object")
        self.detail_json = detail_json
        super().__init__(details["error"])

    @property
    def details(self) -> dict[str, object]:
        """A fresh copy of the service's error, failures and optional metrics."""
        return cast(dict[str, object], json.loads(self.detail_json))


@dataclass(frozen=True)
class RenderOptions:
    """Explicit settings matching the existing /render/grid defaults.

    One angle pair produces a single-view PNG; multiple pairs produce a grid.
    These defaults describe the current API, not a new competition render policy.
    """

    thetas: tuple[float, ...] = (24.0, 120.0, 216.0, 312.0)
    phis: tuple[float, ...] = (-15.0, -15.0, -15.0, -15.0)
    img_size: int = 518
    cam_radius: float = 2.0
    cam_fov_deg: float = 49.1
    gap: int = 5
    bg_color: str = "808080"
    lighting: str = "studio"

    def __post_init__(self) -> None:
        object.__setattr__(self, "thetas", tuple(float(v) for v in self.thetas))
        object.__setattr__(self, "phis", tuple(float(v) for v in self.phis))
        if not self.thetas or len(self.thetas) != len(self.phis):
            raise ValueError("Provide one phi for each theta")
        if not all(math.isfinite(v) for v in (*self.thetas, *self.phis, self.cam_radius, self.cam_fov_deg)):
            raise ValueError("Render angles and camera settings must be finite")
        if type(self.img_size) is not int or self.img_size <= 0:
            raise ValueError("img_size must be a positive integer")
        if type(self.gap) is not int or self.gap < 0:
            raise ValueError("gap must be a nonnegative integer")
        if self.cam_radius <= 0 or not 0 < self.cam_fov_deg < 180:
            raise ValueError("Invalid camera radius or field of view")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", self.bg_color):
            raise ValueError("bg_color must contain six hexadecimal digits")
        object.__setattr__(self, "bg_color", self.bg_color.lower())
        if self.lighting not in ("studio", "follow", "neutral"):
            raise ValueError("Unknown lighting mode")

    def query(self) -> dict[str, str]:
        return {
            "thetas": ",".join(str(v) for v in self.thetas),
            "phis": ",".join(str(v) for v in self.phis),
            "img_size": str(self.img_size),
            "cam_radius": str(self.cam_radius),
            "cam_fov_deg": str(self.cam_fov_deg),
            "gap": str(self.gap),
            "bg_color": self.bg_color,
            "lighting": self.lighting,
        }


DEFAULT_OPTIONS = RenderOptions()


@dataclass(frozen=True)
class RenderRequest:
    renderer_id: str
    source_sha256: str
    options: RenderOptions

    @classmethod
    def from_source(cls, renderer_id: str, source: str, options: RenderOptions) -> Self:
        if not renderer_id:
            raise ValueError("renderer_id must identify the pinned renderer image")
        encoded = source.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES:
            raise ValueError("Source exceeds the renderer's 1 MiB limit")
        return cls(renderer_id, hashlib.sha256(encoded).hexdigest(), options)


class Renderer(Protocol):
    """The miner depends on this interface; validation supplies its implementation."""

    @property
    def renderer_id(self) -> str: ...

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes: ...


class RendererClient:
    """Call an already-running JS renderer. No container management or automatic retries."""

    def __init__(
        self,
        url: str,
        *,
        renderer_id: str,
        api_key: str | None = None,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
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
        self._http = httpx.Client(
            headers=headers, timeout=timeout, transport=transport, trust_env=False, follow_redirects=False
        )

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        RenderRequest.from_source(self.renderer_id, source, options)
        try:
            with self._http.stream("POST", self._url, params=options.query(), json={"source": source}) as response:
                if response.status_code == 422:
                    body = _read_bounded(response, MAX_ERROR_BYTES)
                    try:
                        rejection = RenderRejected(body.decode("utf-8"))
                    except (UnicodeError, ValueError) as error:
                        raise RenderUnavailable("Invalid renderer rejection response") from error
                    raise rejection
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0].lower() != "image/png":
                    raise RenderUnavailable("Renderer did not return image/png")
                png = _read_bounded(response, MAX_RESPONSE_BYTES)
        except httpx.HTTPError as error:
            raise RenderUnavailable(f"Renderer request failed: {error}") from error
        if not png.startswith(PNG_SIGNATURE):
            raise RenderUnavailable("Renderer returned an invalid PNG signature")
        return png

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        if len(body) + len(chunk) > limit:
            raise RenderUnavailable("Renderer response exceeds the size limit")
        body.extend(chunk)
    return bytes(body)
