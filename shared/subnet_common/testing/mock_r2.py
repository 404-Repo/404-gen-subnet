from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self


@dataclass
class MockR2Client:
    """Controllable mock for R2Client."""

    uploads: list[dict[str, object]] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self.uploads.append({"key": key, "data": data, "content_type": content_type})
        return key
