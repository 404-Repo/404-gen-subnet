from typing import Literal
from uuid import UUID, uuid4

from miner_runtime.pod import IMAGE_PATTERN
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class RoundProgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository: str
    branch: str
    round: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    number: int = Field(ge=0)
    prompt_ids: tuple[str, ...] = Field(min_length=1, validation_alias=AliasChoices("prompt_ids", "prompts"))
    generation_deadline_block: int | None = Field(ge=0)
    storage_prefix: str

    @field_validator("prompt_ids", mode="before")
    @classmethod
    def read_prompt_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(row.get("id") if isinstance(row, dict) else row for row in value)
        return value


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    generation_image: str = Field(pattern=IMAGE_PATTERN)
    inputs: RoundProgress
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

    @model_validator(mode="before")
    @classmethod
    def read_snapshot(cls, value: object) -> object:
        if isinstance(value, dict) and "leader" in value:
            return {key: item for key, item in value.items() if key != "leader"}
        return value


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
    legacy_control_token: str | None = Field(
        default=None, validation_alias="control_token", min_length=43, max_length=128, repr=False, exclude=True
    )
    stopped: bool = False
    allocations: list[Allocation] = Field(default_factory=list)
    legacy_published: bool = Field(default=False, validation_alias="published", exclude=True, repr=False)
    completed_prompts: int = Field(default=0, ge=0)
    last_progress_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    progress_warning_sent: bool = False

    @property
    def completed(self) -> bool:
        return self.completed_prompts == len(self.snapshot.inputs.prompt_ids)

    @property
    def allocation(self) -> Allocation | None:
        return self.allocations[-1] if self.allocations else None
