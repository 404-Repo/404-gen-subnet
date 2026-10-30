"""In-memory render observations matched by inputs during replay."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from .client import (
    DEFAULT_OPTIONS,
    PNG_SIGNATURE,
    Renderer,
    RendererError,
    RenderOptions,
    RenderRejected,
    RenderRequest,
)


class ReplayMismatch(RendererError):
    """The pipeline's render inputs do not match a remaining observation."""


@dataclass(frozen=True)
class RenderRecord:
    request: RenderRequest
    png: bytes | None = None
    rejection_json: str | None = None

    def __post_init__(self) -> None:
        if (self.png is None) == (self.rejection_json is None):
            raise ValueError("A render record must contain either a PNG or a rejection")
        if self.png is not None and (not isinstance(self.png, bytes) or not self.png.startswith(PNG_SIGNATURE)):
            raise ValueError("Expected recorded PNG bytes")
        if self.rejection_json is not None:
            RenderRejected(self.rejection_json)


class RecordingRenderer:
    """Record successful PNGs and deterministic rejections from another Renderer.

    Infrastructure failures propagate without being converted to replay observations.
    Concurrent renders are recorded as they complete; persistence/provenance belongs to validation.
    """

    def __init__(self, renderer: Renderer) -> None:
        self._renderer = renderer
        self.renderer_id = renderer.renderer_id
        self._records: list[RenderRecord] = []
        self._incomplete = False

    @property
    def records(self) -> tuple[RenderRecord, ...]:
        if self._incomplete:
            raise RendererError("Cannot replay a recording interrupted by a renderer failure")
        return tuple(self._records)

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        request = RenderRequest.from_source(self.renderer_id, source, options)
        try:
            png = self._renderer.render(source, options)
        except RenderRejected as rejection:
            self._records.append(RenderRecord(request, rejection_json=rejection.detail_json))
            raise
        except Exception:
            self._incomplete = True
            raise
        self._records.append(RenderRecord(request, png=png))
        return png


class ReplayRenderer:
    """Return recorded observations after checking inputs; never contacts a renderer.

    This checks correspondence, not the authenticity or visual correctness of records.
    The validator must establish those before supplying the records to replay.
    """

    def __init__(self, records: Iterable[RenderRecord], *, renderer_id: str) -> None:
        if not renderer_id:
            raise ValueError("renderer_id must identify the pinned renderer image")
        self.renderer_id = renderer_id
        self._records: dict[RenderRequest, deque[RenderRecord]] = {}
        for record in records:
            self._records.setdefault(record.request, deque()).append(record)

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        request = RenderRequest.from_source(self.renderer_id, source, options)
        try:
            record = self._records[request].popleft()
        except (KeyError, IndexError) as error:
            if any(self._records.values()):
                raise ReplayMismatch("Render inputs differ from remaining observations") from error
            raise ReplayMismatch("No recorded render remains") from error
        if record.rejection_json is not None:
            raise RenderRejected(record.rejection_json)
        return cast(bytes, record.png)  # RenderRecord enforces exactly one outcome.

    def assert_consumed(self) -> None:
        remaining = sum(len(records) for records in self._records.values())
        if remaining:
            raise ReplayMismatch(f"{remaining} recorded render(s) were not consumed")
