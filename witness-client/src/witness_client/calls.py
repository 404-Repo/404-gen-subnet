"""Versioned identities shared by recording, worker authorization and replay."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_sha256: Digest
    source_sha256: Digest
    seed: int
    round: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    prompt_url: str | None = Field(default=None, max_length=4096)

    @classmethod
    def from_prompt(
        cls,
        image: bytes,
        seed: int,
        source_sha256: str,
        *,
        round_number: str,
        prompt_url: str | None = None,
    ) -> Generation:
        return cls(
            prompt_sha256=hashlib.sha256(image).hexdigest(),
            source_sha256=source_sha256,
            seed=seed,
            round=round_number,
            prompt_url=prompt_url,
        )

    @property
    def id(self) -> str:
        # URLs are descriptive: identical frozen bytes do not change identity
        # when a signed download URL expires or its query string changes.
        return hashlib.sha256(
            b"404-generate/v1\n" + canonical(self.model_dump(exclude={"prompt_url"}))
        ).hexdigest()


class RequestKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Literal["api", "codex"]
    request_sha256: Digest
    witness_seed: int = 0

    @classmethod
    def from_body(
        cls, provider: Literal["api", "codex"], body: bytes, witness_seed: int = 0
    ) -> RequestKey:
        return cls(
            provider=provider,
            request_sha256=request_digest(body),
            witness_seed=witness_seed,
        )

    @property
    def id(self) -> str:
        return hashlib.sha256(
            b"404-call/v1\n" + canonical(self.model_dump())
        ).hexdigest()


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    generation: Generation
    ordinal: int = Field(ge=0, lt=4096)
    request: RequestKey
