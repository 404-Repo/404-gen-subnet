"""Terminal candidate-validation results, produced by the validator and read by the judge.

The enclosing round path and hotkey map key identify the validation pass. Absence
means validation is pending; infrastructure errors must not be written as failures.
"""

from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


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
