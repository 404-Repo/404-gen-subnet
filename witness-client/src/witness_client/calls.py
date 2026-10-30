"""Logical model-call identity, independent of executions and arrival order."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_serializer

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate request field")
        result[key] = value
    return result


def request_digest(body: bytes) -> str:
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("Request exceeds 8 MiB")
    value = json.loads(body, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ValueError("Request must be a JSON object")  # noqa: TRY004 - invalid JSON shape
    return hashlib.sha256(canonical(value)).hexdigest()


class RequestKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Literal["api", "codex"]
    request_sha256: Digest
    request_id: int = 0
    session_id: str | None = None

    @model_serializer
    def serialize(self) -> dict[str, object]:
        value: dict[str, object] = {
            "provider": self.provider,
            "request_sha256": self.request_sha256,
            "request_id": self.request_id,
        }
        if self.session_id is not None:
            value["session_id"] = self.session_id
        return value

    @classmethod
    def from_body(
        cls,
        provider: Literal["api", "codex"],
        body: bytes,
        request_id: int = 0,
        *,
        session_id: str | None = None,
    ) -> RequestKey:
        return cls(
            provider=provider,
            request_sha256=request_digest(body),
            request_id=request_id,
            session_id=session_id,
        )

    @property
    def id(self) -> str:
        return hashlib.sha256(
            b"404-call/v1\n" + canonical(self.model_dump())
        ).hexdigest()
