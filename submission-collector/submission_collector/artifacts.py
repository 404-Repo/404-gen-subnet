"""Read selected witness outputs from the existing public R2 bucket."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Literal

from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.r2_client import R2Client
from subnet_common.render import GRAY_VIEWS, WHITE_VIEWS


MAX_MANIFEST_BYTES = 256 * 1024
MAX_PNG_BYTES = 32 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ArtifactReadError(RuntimeError):
    """Storage or provenance is unresolved; collection must retry."""


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RoundArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deployment: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    round: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    round_end: int = Field(gt=0)
    profile: Profile
    embedding_revision: str = Field(min_length=1)
    seed: int = Field(ge=0, lt=2**64)
    prompt_hashes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ObjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class SubmissionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: str
    seed: int
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def id(self) -> str:
        return _hash(b"404-generate/v2\n" + _canonical(self.model_dump()))


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    scope_key: str
    generation: SubmissionContext
    submission_commit: str
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_at: float = Field(ge=0, allow_inf_nan=False)
    output_is_none: bool
    artifact_id: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    source: ObjectRef | None
    sealed: bool = False
    missing: bool = False


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    artifact_id: str
    scope_key: str
    generation: SubmissionContext
    submission_commit: str
    profile: Profile
    options: dict[str, object]
    source: ObjectRef
    png: ObjectRef | None
    rejection_json: str | None
    validation: Literal["passed", "rejected"]


@dataclass(frozen=True)
class SelectedOutput:
    source: bytes | None = None
    source_key: str | None = None
    views_key: str | None = None
    failure_reason: str | None = None
    revision: str | None = None
    source_sha256: str | None = None


class WitnessArtifacts:
    def __init__(self, r2: R2Client, round: RoundArtifacts) -> None:
        self.r2, self.round = r2, round

    @classmethod
    async def for_round(cls, r2: R2Client, round_name: str) -> WitnessArtifacts:
        import re

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round_name):
            raise ArtifactReadError("Invalid current round")
        try:
            raw = await r2.download(f"artifacts/v1/rounds/{round_name}.json", limit=MAX_MANIFEST_BYTES)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise ArtifactReadError("Current round artifact metadata is not available") from error
            raise
        round = RoundArtifacts.model_validate_json(raw)
        if round.round != round_name:
            raise ArtifactReadError("Artifact metadata belongs to another round")
        return cls(r2, round)

    async def _get(self, key: str, limit: int) -> bytes | None:
        try:
            return await self.r2.download(key, limit=limit)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    async def _object(self, ref: ObjectRef, expected_key: str, limit: int) -> bytes:
        if ref.key != expected_key or ref.size > limit:
            raise ArtifactReadError("Selected object is outside its scope or size limit")
        raw = await self._get(ref.key, limit)
        if raw is None or len(raw) != ref.size or _hash(raw) != ref.sha256:
            raise ArtifactReadError("Selected artifact bytes are missing or changed")
        return raw

    async def _seal(self, key: str, scope: str, generation: SubmissionContext, commit: str) -> Choice:
        for _ in range(16):
            try:
                raw, etag = await self.r2.download_version(key, MAX_MANIFEST_BYTES)
            except ClientError as error:
                if error.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                    raise
                raw, etag = None, None
            if raw is None:
                missing_operation = _hash(_canonical({"sealed_missing": generation.id}))
                choice = Choice(
                    version=1,
                    scope_key=scope,
                    generation=generation,
                    submission_commit=commit,
                    revision=missing_operation,
                    operation_id=missing_operation,
                    accepted_at=0.0,
                    output_is_none=True,
                    artifact_id=None,
                    source=None,
                    sealed=True,
                    missing=True,
                )
            else:
                choice = Choice.model_validate_json(raw)
                if (
                    choice.scope_key != scope
                    or choice.generation.id != generation.id
                    or choice.submission_commit != commit
                ):
                    raise ArtifactReadError("Final choice does not match the frozen competition input")
                if choice.missing:
                    expected_missing = _hash(_canonical({"sealed_missing": generation.id}))
                    if (
                        not choice.sealed
                        or choice.accepted_at != 0
                        or not choice.output_is_none
                        or choice.source is not None
                        or choice.artifact_id is not None
                        or choice.operation_id != expected_missing
                        or choice.revision != expected_missing
                    ):
                        raise ArtifactReadError("Invalid missing-output collection marker")
                elif not 0 < choice.accepted_at < self.round.round_end:
                    raise ArtifactReadError("Final choice was not accepted before cutoff")
                if choice.sealed:
                    return choice
                choice = choice.model_copy(update={"sealed": True})
            if await self.r2.compare_and_swap(key, choice.model_dump_json().encode(), etag):
                return choice
        raise ArtifactReadError("Final choice changed while collection was sealing it")

    async def selected(
        self,
        hotkey: str,
        submission: MinerSubmission,
        image: bytes,
        seed: int,
        max_source_bytes: int,
    ) -> SelectedOutput:
        if time.time() < self.round.round_end + 300:
            raise ArtifactReadError("Witness final choices have not reached the collection boundary")
        # Validate path components before using a chain-derived scope.
        import re

        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", hotkey) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}", submission.round
        ):
            raise ArtifactReadError("Invalid selected-output scope")
        prompt_hash = _hash(image)
        if submission.round != self.round.round:
            raise ArtifactReadError("Selected-output inputs differ from the current round")
        scope = f"pod-gateway/v1/{self.round.deployment}/{submission.round}/{hotkey}.json"
        root = f"artifacts/v1/{self.round.deployment}/{submission.round}/{hotkey}/{prompt_hash}"
        expected_generation = SubmissionContext(round=submission.round, prompt_sha256=prompt_hash, seed=seed)
        choice = await self._seal(f"{root}/final.json", scope, expected_generation, submission.commit)
        if choice.missing:
            return SelectedOutput(failure_reason="not_submitted", revision=choice.revision)
        if (
            choice.scope_key != scope
            or submission.round != self.round.round
            or choice.generation.id != expected_generation.id
            or choice.submission_commit != submission.commit
            or choice.accepted_at >= self.round.round_end
        ):
            raise ArtifactReadError("Final choice does not match the frozen competition input")
        expected_operation = _hash(
            _canonical(
                {
                    "generation": expected_generation.id,
                    "source_sha256": choice.source.sha256 if choice.source else None,
                }
            )
        )
        if choice.operation_id != expected_operation:
            raise ArtifactReadError("Final choice operation differs")
        if choice.output_is_none:
            if choice.source is not None or choice.artifact_id is not None:
                raise ArtifactReadError("Invalid declared generation failure")
            return SelectedOutput(failure_reason="generation_failed", revision=choice.revision)
        if choice.source is None or choice.artifact_id is None:
            raise ArtifactReadError("Final choice has no selected source")
        raw = await self._get(f"{root}/renders/{choice.artifact_id}.json", MAX_MANIFEST_BYTES)
        if raw is None:
            raise ArtifactReadError("Selected render manifest is unavailable")
        artifact = Artifact.model_validate_json(raw)
        if (
            artifact.scope_key != scope
            or artifact.generation.id != expected_generation.id
            or artifact.submission_commit != submission.commit
            or artifact.artifact_id != choice.artifact_id
            or artifact.profile != self.round.profile
            or artifact.source != choice.source
        ):
            raise ArtifactReadError("Selected render provenance differs")
        expected_id = _hash(
            _canonical(
                {
                    "generation_id": expected_generation.id,
                    "profile": _hash(_canonical(artifact.profile.model_dump())),
                    "request": {
                        "renderer_id": artifact.profile.image_digest,
                        "source_sha256": artifact.source.sha256,
                        "options": artifact.options,
                    },
                }
            )
        )
        if artifact.artifact_id != expected_id:
            raise ArtifactReadError("Selected render identity differs")
        source = await self._object(choice.source, f"{root}/sources/{choice.source.sha256}.js", max_source_bytes)
        source.decode("utf-8")
        binding = _hash(
            _canonical(
                {
                    "profile": _hash(_canonical(self.round.profile.model_dump())),
                    "embedding_revision": self.round.embedding_revision,
                }
            )
        )
        bundle_root = f"{root}/judging/{choice.source.sha256}/{binding}"
        raw = await self._get(f"{bundle_root}/bundle.json", MAX_MANIFEST_BYTES)
        if raw is None:
            raise ArtifactReadError("Pod judging bundle is not complete")
        bundle = json.loads(raw)
        if (
            not isinstance(bundle, dict)
            or bundle.get("version") != 1
            or bundle.get("scope_key") != scope
            or bundle.get("generation") != expected_generation.model_dump()
            or bundle.get("submission_commit") != submission.commit
            or bundle.get("source_sha256") != choice.source.sha256
            or bundle.get("profile") != self.round.profile.model_dump()
            or bundle.get("embedding_revision") != self.round.embedding_revision
        ):
            raise ArtifactReadError("Pod judging bundle provenance differs")
        failure = bundle.get("failure_reason")
        files = bundle.get("files")
        if failure == "render_rejected" and files == {}:
            return SelectedOutput(
                source=source,
                source_key=choice.source.key,
                failure_reason=failure,
                revision=choice.revision,
                source_sha256=choice.source.sha256,
            )
        expected_files = (
            {f"white/{v.name}.png" for v in WHITE_VIEWS}
            | {f"gray/{v.name}.png" for v in GRAY_VIEWS}
            | {"grid.png", "embeddings.npz"}
        )
        if failure is not None or not isinstance(files, dict) or files.keys() != expected_files:
            raise ArtifactReadError("Pod judging bundle is incomplete")
        for name, data in files.items():
            ref = ObjectRef.model_validate(data)
            content = await self._object(ref, f"{bundle_root}/{name}", MAX_PNG_BYTES)
            if name.endswith(".png") and not content.startswith(PNG_SIGNATURE):
                raise ArtifactReadError("Pod judging view is not a PNG")
        return SelectedOutput(
            source=source,
            source_key=choice.source.key,
            views_key=bundle_root,
            revision=choice.revision,
            source_sha256=choice.source.sha256,
        )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
