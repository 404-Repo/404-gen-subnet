from enum import Enum

from pydantic import BaseModel, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class VerificationVerdict(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"


class VerificationResult(BaseModel):
    """Final verification result for a miner."""

    hotkey: str
    verdict: VerificationVerdict
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


def get_verified_hotkeys(results: list[VerificationResult]) -> set[str]:
    return {r.hotkey for r in results if r.verdict == VerificationVerdict.VERIFIED}


def get_failed_hotkeys(results: list[VerificationResult]) -> set[str]:
    return {r.hotkey for r in results if r.verdict == VerificationVerdict.FAILED}
