"""Configuration and storage paths for generated and submitted results."""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter, GenerationsMap

from .models import Scope
from .state import Snapshot, StateConflict, StateStore


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SubmissionContext(Frozen):
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int = Field(ge=0, lt=2**64)


class ArtifactProfile(Frozen):
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ArtifactConfig(Frozen):
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


class ArtifactRepository:
    def __init__(
        self,
        objects: Objects,
        scope: Scope,
        *,
        index: StateStore,
        seed: int,
        prompt_ids: dict[str, str],
        submission_commit: str | None = None,
        storage_prefix: str = "",
    ) -> None:
        self.objects, self.scope, self.index = objects, scope, index
        self.seed, self.submission_commit, self.prompt_ids = seed, submission_commit, prompt_ids
        self.storage_prefix = storage_prefix
        self.index_key = f"{storage_prefix}rounds/{scope.round}/{scope.hotkey}/submitted.json"
        self._saved: Snapshot | None = None

    def require(self, generation: SubmissionContext) -> None:
        if generation.seed != self.seed:
            raise ValueError("Seed differs")
        if generation.prompt_sha256 not in self.prompt_ids:
            raise ValueError("Prompt is not admitted")

    def prefix(self, prompt: str) -> str:
        if prompt not in self.prompt_ids:
            raise ValueError("Prompt is not admitted")
        return f"{self.storage_prefix}rounds/{self.scope.round}/{self.scope.hotkey}/submitted/{self.prompt_ids[prompt]}"

    def result(self, prompt: str) -> GenerationResult | None:
        self.prefix(prompt)
        return self.results().get(self.prompt_ids[prompt])

    def results(self) -> GenerationsMap:
        saved = self.index.load(self.index_key)
        for _ in range(16):
            results = {} if saved is None else GenerationsAdapter.validate_json(saved.data)
            identified = self._identify(results)
            if identified == results:
                return identified
            try:
                self._saved = self.index.save(
                    self.index_key, GenerationsAdapter.dump_json(identified), None if saved is None else saved.etag
                )
                return identified
            except StateConflict:
                saved = self.index.load(self.index_key)
        raise StateConflict("Result index is busy")

    def _identify(self, results: GenerationsMap) -> GenerationsMap:
        official_ids = set(self.prompt_ids.values())
        identified: GenerationsMap = {}
        for key, result in results.items():
            prompt = key if key in official_ids else self.prompt_ids.get(key)
            if prompt is None:
                raise ValueError("Saved prompt is not admitted")
            if prompt in identified and identified[prompt] != result:
                raise ValueError("Saved prompt results differ")
            identified[prompt] = result
        return identified

    def update_result(self, prompt: str, result: GenerationResult) -> None:
        self.prefix(prompt)
        saved = self._saved or self.index.load(self.index_key)
        for _ in range(16):
            results = {} if saved is None else GenerationsAdapter.validate_json(saved.data)
            results = self._identify(results)
            results[self.prompt_ids[prompt]] = result
            try:
                self._saved = self.index.save(
                    self.index_key, GenerationsAdapter.dump_json(results), None if saved is None else saved.etag
                )
                return
            except StateConflict:
                saved = self.index.load(self.index_key)
        raise StateConflict("Result index is busy")
