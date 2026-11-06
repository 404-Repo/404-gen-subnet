"""Frozen generation inputs and admission policy shared with Witness Pods."""

from __future__ import annotations

import hashlib
import re

from pydantic import Field, model_validator

from subnet_common.competition.schedule import RoundSchedule
from subnet_common.competition.state import RoundStage

from .artifacts import Frozen
from .auth import AuthRejected
from .models import json_bytes


def repository(value: str) -> str:
    """Return a canonical GitHub repository identity."""
    if value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/").removesuffix("/")
    value = value.removesuffix(".git")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        raise ValueError("Expected a GitHub repository URL or owner/repository")
    return value.lower()


class PromptImage(Frozen):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
    source_url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_key: str


class RoundInputs(Frozen):
    repository: str
    branch: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    round: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    number: int = Field(ge=0)
    seed: int = Field(ge=0, lt=2**64)
    prompts: tuple[PromptImage, ...] = Field(min_length=1, max_length=4096)
    generation_deadline_block: int | None = Field(ge=0)
    storage_prefix: str
    save_submissions: bool = True

    @model_validator(mode="after")
    def valid_inputs(self) -> RoundInputs:
        if repository(self.repository) != self.repository:
            raise ValueError("Expected canonical competition repository")
        RoundSchedule.storage_prefix(self.storage_prefix)
        if len(self.prompt_ids) != len(self.prompts) or len({p.id for p in self.prompts}) != len(self.prompts):
            raise ValueError("Expected distinct prompt images and official IDs")
        for prompt in self.prompts:
            if prompt.image_key != f"{self.storage_prefix}competition-inputs/v1/{prompt.sha256}":
                raise ValueError("Prompt content reference differs")
        return self

    @property
    def fingerprint(self) -> str:
        # State-only commits can pause and resume the same frozen inputs.
        data = self.model_dump(mode="json", exclude={"revision", "prompts"})
        data["prompts"] = [{"id": p.id, "sha256": p.sha256, "image_key": p.image_key} for p in self.prompts]
        return hashlib.sha256(json_bytes(data)).hexdigest()

    @property
    def prompt_ids(self) -> dict[str, str]:
        return {prompt.sha256: prompt.id for prompt in self.prompts}


class RoundPolicy(Frozen):
    repository: str
    branch: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    round: str
    inputs_sha256: str | None
    stage: str
    observed_at: float
    expires_at: float
    state_expires_at: float | None = None
    block: int | None = None
    generation_deadline_block: int | None = None

    def require(self, inputs: RoundInputs, now: float, *, generation: bool = True) -> None:
        expires = self.expires_at if generation or self.state_expires_at is None else self.state_expires_at
        if not self.observed_at <= now + 5 or not now < expires <= self.observed_at + 120:
            raise AuthRejected("round_policy_stale", 503)
        if (self.repository != inputs.repository or self.branch != inputs.branch or self.round != inputs.round
                or self.inputs_sha256 != inputs.fingerprint):
            raise AuthRejected("round_policy_mismatch", 403)
        if self.generation_deadline_block != inputs.generation_deadline_block:
            raise AuthRejected("round_policy_mismatch", 403)
        if not generation:
            return
        if self.stage != RoundStage.MINER_GENERATION.value:
            raise AuthRejected("round_closed", 403)
        if inputs.generation_deadline_block is not None and (
            self.block is None or self.block >= inputs.generation_deadline_block
        ):
            raise AuthRejected("round_closed", 403)
