"""Durable render artifacts and final choices, independent of model-call witnessing."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from renderer_client import RenderOptions, RenderRequest
from witness_client.calls import Digest, canonical

from .models import Scope
from .state import Snapshot, StateConflict, StateStore, WriteRejected

MAX_PNG_BYTES = 32 * 1024 * 1024
MAX_REJECTION_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_OPTIONS_BYTES = 32 * 1024
RENDER_LEASE_SECONDS = 300
MAX_RENDER_ATTEMPTS = 3


class ArtifactConflict(RuntimeError):
    pass


class ArtifactPending(RuntimeError):
    pass


class ArtifactFailed(RuntimeError):
    pass


class ArtifactLimit(RuntimeError):
    pass


class Objects(Protocol):
    def get(self, key: str, limit: int = ...) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SubmissionContext(Frozen):
    """Frozen input identity for saved artifacts and the independent final submission."""

    round: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    prompt_sha256: Digest
    seed: int = Field(ge=0, lt=2**64)

    @classmethod
    def from_prompt(cls, image: bytes, seed: int, *, round_number: str) -> SubmissionContext:
        return cls(round=round_number, prompt_sha256=hashlib.sha256(image).hexdigest(), seed=seed)

    @property
    def id(self) -> str:
        return hashlib.sha256(b"404-generate/v2\n" + canonical(self.model_dump())).hexdigest()


class ArtifactProfile(Frozen):
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_sha256: Digest
    bootstrap_sha256: Digest | None = None

    @property
    def id(self) -> str:
        return hashlib.sha256(canonical(self.model_dump())).hexdigest()


class ArtifactLimits(Frozen):
    max_renders: int = Field(ge=1, le=256)
    max_bytes: int = Field(gt=0)


class ArtifactConfig(Frozen):
    profile: ArtifactProfile
    limits: ArtifactLimits
    seed: int = Field(ge=0, lt=2**64)
    commits: dict[str, str]
    prompt_hashes: frozenset[Digest] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def submissions(self) -> ArtifactConfig:
        import re

        if not self.commits or any(
            not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            for hotkey, commit in self.commits.items()
        ):
            raise ValueError("Expected frozen hotkey to source-commit bindings")
        if self.limits.max_bytes < MAX_RENDER_ATTEMPTS * (MAX_PNG_BYTES + MAX_MANIFEST_BYTES) + 1024 * 1024:
            raise ValueError("Artifact byte limit cannot reserve one maximum-size render and its recovery attempts")
        return self


class ObjectRef(Frozen):
    key: str
    sha256: Digest
    size: int = Field(ge=0)


class RenderArtifact(Frozen):
    version: Literal[1] = 1
    artifact_id: Digest
    scope_key: str
    generation: SubmissionContext
    submission_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    profile: ArtifactProfile
    options: RenderOptions
    source: ObjectRef
    png: ObjectRef | None = None
    rejection_json: str | None = None
    validation: Literal["passed", "rejected"]

    @model_validator(mode="after")
    def outcome(self) -> RenderArtifact:
        if (self.png is None) == (self.rejection_json is None):
            raise ValueError("A render has one complete outcome")
        if (self.validation == "passed") != (self.png is not None):
            raise ValueError("Validation and render outcome differ")
        return self


class FinalChoice(Frozen):
    version: Literal[1] = 1
    scope_key: str
    generation: SubmissionContext
    submission_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    revision: Digest
    operation_id: Digest
    accepted_at: float
    output_is_none: bool
    artifact_id: Digest | None
    source: ObjectRef | None
    sealed: bool = False
    missing: bool = False

    @model_validator(mode="after")
    def outcome(self) -> FinalChoice:
        if self.output_is_none != (self.source is None) or self.output_is_none != (self.artifact_id is None):
            raise ValueError("Final source and outcome differ")
        if self.missing and (not self.sealed or not self.output_is_none or self.accepted_at != 0):
            raise ValueError("Missing output must be a sealed collection marker")
        return self


class RenderSlot(Frozen):
    generation_id: Digest
    source_sha256: Digest
    reserved_bytes: int
    owner: str
    lease_until: float
    attempts: int = 1


class Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope_key: str
    profile_sha256: Digest
    limits: ArtifactLimits
    seed: int
    submission_commit: str
    renders: dict[str, RenderSlot] = Field(default_factory=dict)


class ArtifactRepository:
    def __init__(
        self,
        state: StateStore,
        objects: Objects,
        scope: Scope,
        profile: ArtifactProfile,
        limits: ArtifactLimits,
        *,
        seed: int,
        submission_commit: str,
        prompt_hashes: frozenset[str],
    ) -> None:
        self.state, self.objects, self.scope = state, objects, scope
        self.profile, self.limits = profile, limits
        self.seed, self.submission_commit = seed, submission_commit
        self.prompt_hashes = prompt_hashes

    def prefix(self, prompt: str) -> str:
        if prompt not in self.prompt_hashes or len(prompt) != 64 or any(c not in "0123456789abcdef" for c in prompt):
            raise ValueError("Invalid prompt hash")
        return f"artifacts/v1/{self.scope.deployment}/{self.scope.round}/{self.scope.hotkey}/{prompt}"

    def _generation(self, generation: SubmissionContext) -> None:
        if generation.round != self.scope.round or generation.seed != self.seed:
            raise ValueError("Artifact round or seed differs")
        if generation.prompt_sha256 not in self.prompt_hashes:
            raise ValueError("Artifact prompt is not admitted")

    def _save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        for attempt in range(4):
            try:
                return self.state.save(key, data, expected)
            except WriteRejected:
                if attempt == 3:
                    raise
                # R2 limits writes to one key per second. Preconditions remain intact.
                time.sleep(1.1 * (attempt + 1))
        raise ArtifactPending("Artifact storage is busy")

    def identity(self, generation: SubmissionContext, source: str, options: RenderOptions) -> str:
        self._generation(generation)
        request = RenderRequest.from_source(self.profile.image_digest, source, options)
        if len(canonical(asdict(request))) > MAX_OPTIONS_BYTES:
            raise ValueError("Oversized render settings")
        return hashlib.sha256(
            canonical(
                {
                    "generation_id": generation.id,
                    "profile": self.profile.id,
                    "request": asdict(request),
                }
            )
        ).hexdigest()

    def _manifest_key(self, prompt: str, artifact_id: str) -> str:
        return f"{self.prefix(prompt)}/renders/{artifact_id}.json"

    def load_render(self, generation: SubmissionContext, artifact_id: str) -> RenderArtifact | None:
        self._generation(generation)
        if len(artifact_id) != 64 or any(c not in "0123456789abcdef" for c in artifact_id):
            raise ValueError("Invalid artifact reference")
        raw = self.objects.get(self._manifest_key(generation.prompt_sha256, artifact_id), MAX_MANIFEST_BYTES)
        if raw is None:
            return None
        artifact = RenderArtifact.model_validate_json(raw)
        if (
            artifact.scope_key != self.scope.key
            or artifact.generation.id != generation.id
            or artifact.artifact_id != artifact_id
            or artifact.profile != self.profile
            or artifact.submission_commit != self.submission_commit
            or artifact.source.key != f"{self.prefix(generation.prompt_sha256)}/sources/{artifact.source.sha256}.js"
        ):
            raise ArtifactConflict("Artifact context differs")
        expected_id = hashlib.sha256(
            canonical(
                {
                    "generation_id": generation.id,
                    "profile": self.profile.id,
                    "request": asdict(
                        RenderRequest(self.profile.image_digest, artifact.source.sha256, artifact.options)
                    ),
                }
            )
        ).hexdigest()
        if artifact_id != expected_id:
            raise ArtifactConflict("Artifact identity differs")
        if artifact.png is not None and artifact.png.key != (
            f"{self.prefix(generation.prompt_sha256)}/images/{artifact.png.sha256}.png"
        ):
            raise ArtifactConflict("Artifact PNG context differs")
        return artifact

    def _inventory(self, prompt: str) -> tuple[Inventory, str | None]:
        saved = self.state.load(f"{self.prefix(prompt)}/inventory.json")
        inventory = (
            Inventory.model_validate_json(saved.data)
            if saved
            else Inventory(
                scope_key=self.scope.key,
                profile_sha256=self.profile.id,
                limits=self.limits,
                seed=self.seed,
                submission_commit=self.submission_commit,
            )
        )
        if (
            inventory.scope_key != self.scope.key
            or inventory.profile_sha256 != self.profile.id
            or inventory.limits != self.limits
            or inventory.seed != self.seed
            or inventory.submission_commit != self.submission_commit
        ):
            raise ArtifactConflict("Artifact profile or limits changed")
        return inventory, saved.etag if saved else None

    def reserve(self, generation: SubmissionContext, source: str, options: RenderOptions) -> str:
        artifact_id = self.identity(generation, source, options)
        prompt = generation.prompt_sha256
        reservation = len(source.encode()) + MAX_RENDER_ATTEMPTS * (MAX_PNG_BYTES + MAX_MANIFEST_BYTES)
        owner = uuid.uuid4().hex
        for _ in range(16):
            inventory, etag = self._inventory(prompt)
            previous = inventory.renders.get(artifact_id)
            if previous is not None:
                if previous.lease_until > time.time():
                    raise ArtifactPending("Original render is pending")
                if previous.attempts >= MAX_RENDER_ATTEMPTS:
                    raise ArtifactFailed("Render recovery budget is exhausted")
            if previous is None and (
                len(inventory.renders) >= self.limits.max_renders
                or sum(r.reserved_bytes for r in inventory.renders.values()) + reservation > self.limits.max_bytes
            ):
                raise ArtifactLimit("Prompt artifact storage limit reached")
            inventory.renders[artifact_id] = RenderSlot(
                generation_id=generation.id,
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                reserved_bytes=reservation,
                owner=owner,
                lease_until=time.time() + RENDER_LEASE_SECONDS,
                attempts=previous.attempts + 1 if previous else 1,
            )
            try:
                self._save(f"{self.prefix(prompt)}/inventory.json", inventory.model_dump_json().encode(), etag)
                return owner
            except StateConflict:
                continue
        raise ArtifactPending("Artifact inventory is busy")

    def complete(
        self,
        generation: SubmissionContext,
        source: str,
        options: RenderOptions,
        *,
        owner: str,
        png: bytes | None = None,
        rejection_json: str | None = None,
    ) -> RenderArtifact:
        artifact_id = self.identity(generation, source, options)
        saved = self.load_render(generation, artifact_id)
        if saved is not None:
            return saved
        inventory, _ = self._inventory(generation.prompt_sha256)
        slot = inventory.renders[artifact_id]
        if slot.owner != owner:
            raise ArtifactPending("Render ownership changed during recovery")
        root = self.prefix(generation.prompt_sha256)
        source_bytes = source.encode()
        source_ref = self._put(f"{root}/sources/{hashlib.sha256(source_bytes).hexdigest()}.js", source_bytes)
        png_ref = None
        if png is not None:
            if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > MAX_PNG_BYTES:
                raise ValueError("Invalid renderer PNG")
            png_ref = self._put(f"{root}/images/{hashlib.sha256(png).hexdigest()}.png", png)
        if rejection_json is not None and len(rejection_json.encode()) > MAX_REJECTION_BYTES:
            raise ValueError("Oversized rejection")
        artifact = RenderArtifact(
            artifact_id=artifact_id,
            scope_key=self.scope.key,
            generation=generation,
            submission_commit=self.submission_commit,
            profile=self.profile,
            options=options,
            source=source_ref,
            png=png_ref,
            rejection_json=rejection_json,
            validation="passed" if png is not None else "rejected",
        )
        raw = artifact.model_dump_json().encode()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Oversized render manifest")
        # Objects precede the manifest; incomplete uploads never become saved renders.
        # Conditional publication makes the first complete result immutable, even
        # if an old worker finishes after another worker recovered its lease.
        try:
            self._save(self._manifest_key(generation.prompt_sha256, artifact_id), raw, None)
        except StateConflict:
            winner = self.load_render(generation, artifact_id)
            if winner is None:
                raise ArtifactPending("Saved render publication is unresolved") from None
            return winner
        return artifact

    def release_reservation(self, artifact: RenderArtifact) -> None:
        prompt = artifact.generation.prompt_sha256
        actual_size = artifact.source.size + (artifact.png.size if artifact.png else 0)
        actual_size += len(artifact.model_dump_json().encode())
        for _ in range(16):
            inventory, etag = self._inventory(prompt)
            slot = inventory.renders[artifact.artifact_id]
            # Reserve for orphaned results from interrupted attempts until retention.
            size = actual_size + (slot.attempts - 1) * (MAX_PNG_BYTES + MAX_MANIFEST_BYTES)
            if slot.reserved_bytes == size:
                return
            inventory.renders[artifact.artifact_id] = slot.model_copy(update={"reserved_bytes": size})
            try:
                self._save(f"{self.prefix(prompt)}/inventory.json", inventory.model_dump_json().encode(), etag)
                return
            except StateConflict:
                continue
        raise ArtifactPending("Artifact inventory is busy")

    def _put(self, key: str, data: bytes) -> ObjectRef:
        if self.objects.get(key, len(data)) != data:
            self.objects.put(key, data)
        return ObjectRef(key=key, sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def read_object(self, ref: ObjectRef, limit: int) -> bytes:
        raw = self.objects.get(ref.key, limit)
        if raw is None or len(raw) != ref.size or hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ArtifactPending("Saved artifact bytes are missing or changed")
        return raw

    def find_render(self, generation: SubmissionContext, source_sha256: str) -> RenderArtifact | None:
        inventory, _ = self._inventory(generation.prompt_sha256)
        for artifact_id, slot in inventory.renders.items():
            if slot.generation_id == generation.id and slot.source_sha256 == source_sha256:
                artifact = self.load_render(generation, artifact_id)
                if artifact is not None:
                    return artifact
        return None

    @staticmethod
    def operation(generation: SubmissionContext, source_sha256: str | None) -> str:
        return hashlib.sha256(canonical({"generation": generation.id, "source_sha256": source_sha256})).hexdigest()

    def load_final(self, prompt: str) -> FinalChoice | None:
        saved = self.state.load(f"{self.prefix(prompt)}/final.json")
        if saved is None:
            return None
        choice = FinalChoice.model_validate_json(saved.data)
        if (
            choice.scope_key != self.scope.key
            or choice.generation.prompt_sha256 != prompt
            or choice.submission_commit != self.submission_commit
        ):
            raise ArtifactConflict("Final choice context differs")
        self._generation(choice.generation)
        return choice

    def collect_final(self, prompt: str) -> tuple[FinalChoice, bytes | None, RenderArtifact | None]:
        """Resolve the selected source directly from artifact storage for a trusted consumer."""
        choice = self.load_final(prompt)
        if choice is None:
            raise ArtifactPending("No final choice has been saved")
        if choice.output_is_none:
            return choice, None, None
        if choice.source is None or choice.artifact_id is None:
            raise ArtifactConflict("Final choice has no source")
        artifact = self.load_render(choice.generation, choice.artifact_id)
        if artifact is None or artifact.source != choice.source:
            raise ArtifactPending("Selected render is unavailable")
        return choice, self.read_object(choice.source, 1024 * 1024), artifact

    def save_final(
        self,
        generation: SubmissionContext,
        source_sha256: str | None,
        expected_revision: str | None,
        artifact: RenderArtifact | None,
        *,
        accepted_at: float | None = None,
        round_end: float | None = None,
    ) -> FinalChoice:
        self._generation(generation)
        operation = self.operation(generation, source_sha256)
        key = f"{self.prefix(generation.prompt_sha256)}/final.json"
        saved = self.state.load(key)
        previous = FinalChoice.model_validate_json(saved.data) if saved else None
        if previous is not None and previous.operation_id == operation:
            return previous
        if previous is not None and previous.sealed:
            raise ArtifactConflict("Final choice is sealed for collection")
        if (previous.revision if previous else None) != expected_revision:
            raise ArtifactConflict("Final choice changed; supply its current revision to replace it")
        if source_sha256 is not None:
            if (
                artifact is None
                or artifact.source.sha256 != source_sha256
                or artifact.scope_key != self.scope.key
                or artifact.generation.id != generation.id
            ):
                raise ArtifactConflict("Final source does not match its saved render")
            self.read_object(artifact.source, 1024 * 1024)
        if round_end is not None:
            accepted_at = time.time()
            if accepted_at >= round_end:
                raise ArtifactConflict("Round closed before final choice publication")
        choice = FinalChoice(
            scope_key=self.scope.key,
            generation=generation,
            submission_commit=self.submission_commit,
            operation_id=operation,
            revision=hashlib.sha256(canonical({"operation": operation, "previous": expected_revision})).hexdigest(),
            accepted_at=time.time() if accepted_at is None else accepted_at,
            output_is_none=source_sha256 is None,
            artifact_id=artifact.artifact_id if artifact else None,
            source=artifact.source if artifact else None,
        )
        try:
            self._save(key, choice.model_dump_json().encode(), saved.etag if saved else None)
        except StateConflict:
            current = self.load_final(generation.prompt_sha256)
            if current is not None and current.operation_id == operation:
                return current
            raise ArtifactConflict("Another final choice won the conditional write") from None
        return choice
