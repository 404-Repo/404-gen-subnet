"""Terminal candidate-validation results, produced by the validator and read by the judge.

The enclosing round path and hotkey map key identify the validation pass. Absence
means validation is pending; infrastructure errors must not be written as failures.
"""

from decimal import Decimal
from enum import Enum
from typing import Literal, Self

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    model_serializer,
    model_validator,
)

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class PromptProgress(BaseModel):
    status: Literal["pending", "passed", "failed"] = Field(
        default="pending", validation_alias=AliasChoices("status", "verdict")
    )
    attempts: int = Field(default=0, ge=0, le=3)
    deadline: AwareDatetime | None = None
    retryable: bool = True
    reason: str = ""
    charges: dict[str, Decimal | None] = Field(default_factory=dict)

    @property
    def consumed_calls(self) -> int:
        return len(self.charges)

    @property
    def consumed_credits(self) -> Decimal:
        return sum((value for value in self.charges.values() if value is not None), Decimal(0))

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        return {
            **handler(self),
            "consumed_calls": self.consumed_calls,
            "consumed_credits": str(self.consumed_credits),
        }


class ValidationProgress(BaseModel):
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    prompts: dict[str, PromptProgress] = Field(default_factory=dict)


class ValidationVerdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: ValidationVerdict
    reason: str = ""

    @model_validator(mode="after")
    def require_failure_reason(self) -> Self:
        if self.verdict == ValidationVerdict.FAILED and not self.reason.strip():
            raise ValueError("Failed validation requires a reason")
        return self


ValidationResultsAdapter = TypeAdapter(dict[str, ValidationResult])


async def get_validation_results(git: GitHubClient, round_num: int, ref: str) -> dict[str, ValidationResult]:
    content = await git.get_file(f"rounds/{round_num}/validation_results.json", ref=ref)
    if content is None:
        return {}
    return ValidationResultsAdapter.validate_json(content)


async def save_validation_results(
    git_batcher: GitBatcher, round_num: int, results: dict[str, ValidationResult]
) -> None:
    """Publish the round's terminal result map; callers retain existing results."""
    await git_batcher.write(
        path=f"rounds/{round_num}/validation_results.json",
        content=ValidationResultsAdapter.dump_json(results, indent=2).decode(),
        message=f"Update validation results for round {round_num}",
    )
