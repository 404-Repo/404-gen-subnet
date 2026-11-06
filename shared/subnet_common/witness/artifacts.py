"""Shared artifact configuration and object-store contracts."""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    profile: ArtifactProfile
    seed: int = Field(ge=0, lt=2**64)
    commits: dict[str, str] = Field(default_factory=dict)
    prompt_ids: dict[str, str] = Field(min_length=1, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def legacy_prompts(cls, value: object) -> object:
        if isinstance(value, dict) and "prompt_ids" not in value:
            hashes = value.get("prompt_hashes")
            if isinstance(hashes, (list, tuple, set, frozenset)) and all(isinstance(key, str) for key in hashes):
                return {**value, "prompt_ids": {key: key for key in hashes}}
        return value

    @model_validator(mode="after")
    def submissions(self) -> ArtifactConfig:
        if any(
            not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            for hotkey, commit in self.commits.items()
        ):
            raise ValueError("Expected hotkeys and source commits")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", prompt)
            for digest, prompt in self.prompt_ids.items()
        ) or len(set(self.prompt_ids.values())) != len(self.prompt_ids):
            raise ValueError("Expected image SHA256 hashes mapped to distinct official prompt IDs")
        return self


class Objects(Protocol):
    def get(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None: ...

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
