from dataclasses import dataclass, field


@dataclass
class MockR2Client:
    """Controllable mock for R2Client."""

    uploads: list[dict[str, object]] = field(default_factory=list)

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self.uploads.append({"key": key, "data": data, "content_type": content_type})
        return key
