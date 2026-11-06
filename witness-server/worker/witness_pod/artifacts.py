"""Configuration and storage paths for generated and submitted results."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter, GenerationsMap
from subnet_common.witness.artifacts import Objects
from subnet_common.witness.models import Scope
from subnet_common.witness.state import Snapshot, StateConflict, StateStore


class SubmissionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int = Field(ge=0, lt=2**64)


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
