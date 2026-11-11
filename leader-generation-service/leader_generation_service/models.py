import re
from typing import Literal, Self
from uuid import UUID, uuid4

from miner_runtime.pod import IMAGE_PATTERN
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from subnet_common.competition.leader import LeaderEntry
from subnet_common.witness.rounds import RoundInputs, repository


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    generation_image: str = Field(pattern=IMAGE_PATTERN)
    leader: LeaderEntry
    inputs: RoundInputs
    hotkey: str = Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{48}$")
    account_id: str = Field(pattern=r"^[a-f0-9-]{1,80}$")
    wallet_name: str
    wallet_hotkey: str
    witness_url: str
    workers: int
    parallel_calls: int
    parallel_preparations: int
    prover_contexts: int
    go_cpus: int
    go_memory_mib: int

    @model_validator(mode="after")
    def source(self) -> Self:
        repository(self.leader.repo)
        if re.fullmatch(r"[0-9a-f]{40}", self.leader.commit) is None:
            raise ValueError("Leader source must use a full commit SHA")
        return self


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    pod_id: str | None = None
    requested_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    legacy_worker_token: str | None = Field(
        default=None, validation_alias="token", min_length=43, max_length=128, repr=False, exclude=True
    )
    phase: Literal["intent", "creating", "active", "terminated"] = "intent"

    @field_validator("phase", mode="before")
    @classmethod
    def read_phase(cls, value: object) -> object:
        return "active" if value in ("allocated", "ready", "running") else value

    @property
    def name(self) -> str:
        return f"leader-runner-{self.id}"


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: Snapshot
    control_token: str | None = Field(default=None, min_length=43, max_length=128, repr=False)
    stopped: bool = False
    allocations: list[Allocation] = Field(default_factory=list)
    published: bool = False
    completed_prompts: int = Field(default=0, ge=0)
    last_progress_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    progress_warning_sent: bool = False

    @property
    def allocation(self) -> Allocation | None:
        return self.allocations[-1] if self.allocations else None
