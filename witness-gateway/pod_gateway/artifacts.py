"""Configuration and storage paths for generated and submitted results."""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter

from .models import Scope
from .state import Snapshot, StateConflict, StateStore


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SubmissionContext(Frozen):
    round: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
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
    commits: dict[str, str]
    prompt_hashes: frozenset[str] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def submissions(self) -> ArtifactConfig:
        if not self.commits or any(
            not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            for hotkey, commit in self.commits.items()
        ):
            raise ValueError("Expected admitted hotkeys and source commits")
        if any(not re.fullmatch(r"[0-9a-f]{64}", prompt) for prompt in self.prompt_hashes):
            raise ValueError("Expected prompt hashes")
        return self


class Objects(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...


class ArtifactRepository:
    def __init__(
        self,
        objects: Objects,
        scope: Scope,
        *,
        index: StateStore,
        seed: int,
        submission_commit: str,
        prompt_hashes: frozenset[str],
    ) -> None:
        self.objects, self.scope, self.index = objects, scope, index
        self.seed, self.submission_commit, self.prompt_hashes = seed, submission_commit, prompt_hashes
        self.index_key = f"rounds/{scope.round}/{scope.hotkey}/submitted.json"
        self._saved: Snapshot | None = None

    def require(self, generation: SubmissionContext, commit: str) -> None:
        if generation.round != self.scope.round or generation.seed != self.seed:
            raise ValueError("Round or seed differs")
        if generation.prompt_sha256 not in self.prompt_hashes:
            raise ValueError("Prompt is not admitted")
        if commit != self.submission_commit:
            raise ValueError("Source submission differs")

    def prefix(self, prompt: str) -> str:
        if prompt not in self.prompt_hashes:
            raise ValueError("Prompt is not admitted")
        return f"rounds/{self.scope.round}/{self.scope.hotkey}/submitted/{prompt}"

    def result(self, prompt: str) -> GenerationResult | None:
        self.prefix(prompt)
        saved = self.index.load(self.index_key)
        return None if saved is None else GenerationsAdapter.validate_json(saved.data).get(prompt)

    def update_result(self, prompt: str, result: GenerationResult | None) -> None:
        self.prefix(prompt)
        saved = self._saved or self.index.load(self.index_key)
        for _ in range(16):
            results = {} if saved is None else GenerationsAdapter.validate_json(saved.data)
            if result is None:
                results.pop(prompt, None)
            else:
                results[prompt] = result
            try:
                self._saved = self.index.save(
                    self.index_key, GenerationsAdapter.dump_json(results), None if saved is None else saved.etag
                )
                return
            except StateConflict:
                saved = self.index.load(self.index_key)
        raise StateConflict("Result index is busy")
