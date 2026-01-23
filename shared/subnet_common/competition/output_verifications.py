from enum import Enum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class VerificationVerdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class VerificationResult(BaseModel):
    """
    Final verification result for a miner.

    Tolerances:
    - failed_prompts: up to 10% allowed (non-critical)
    - failed_critical: up to 1% allowed
    - overtime_prompts: counts as failure
    """

    hotkey: str
    verdict: VerificationVerdict
    checked_prompts: int = Field(description="Number of prompts checked (may exit early on failure)")
    failed_prompts: int = Field(description="Non-critical prompts that failed verification")
    failed_critical: int = Field(description="Critical/decisive prompts that failed verification")
    overtime_prompts: int = Field(description="Prompts where regeneration exceeded time limit")
    reason: str = ""


VerificationListAdapter = TypeAdapter(list[VerificationResult])


async def get_output_verifications(git: GitHubClient, round_num: int, ref: str) -> list[VerificationResult]:
    content = await git.get_file(f"rounds/{round_num}/output_verification.json", ref=ref)
    if not content:
        return []
    return VerificationListAdapter.validate_json(content)


async def save_output_verifications(git_batcher: GitBatcher, round_num: int, results: list[VerificationResult]) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/output_verification.json",
        content=VerificationListAdapter.dump_json(results, indent=2).decode(),
        message=f"Update output verifications for round {round_num}",
    )


def get_passed_hotkeys(results: list[VerificationResult]) -> set[str]:
    return {r.hotkey for r in results if r.verdict == VerificationVerdict.PASSED}


def get_failed_hotkeys(results: list[VerificationResult]) -> set[str]:
    return {r.hotkey for r in results if r.verdict == VerificationVerdict.FAILED}
