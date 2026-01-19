from enum import Enum

from pydantic import BaseModel, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class AuditVerdict(str, Enum):
    PASSED = "passed"
    DISQUALIFIED = "disqualified"


class AuditResult(BaseModel):
    """
    Source code audit result for a miner.

    Checks may include:
    - License compliance
    - Reproducibility
    - Malware/security scan
    - Banned patterns (hardcoded outputs, etc.)
    """

    hotkey: str
    verdict: AuditVerdict
    reason: str = ""


AuditListAdapter = TypeAdapter(list[AuditResult])


async def get_source_audits(git: GitHubClient, round_num: int, ref: str) -> list[AuditResult]:
    content = await git.get_file(f"rounds/{round_num}/source_audit.json", ref=ref)
    if not content:
        return []
    return AuditListAdapter.validate_json(content)


async def save_source_audits(git_batcher: GitBatcher, round_num: int, results: list[AuditResult]) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/source_audit.json",
        content=AuditListAdapter.dump_json(results, indent=2).decode(),
        message=f"Update source audits for round {round_num}",
    )


def get_passed_hotkeys(results: list[AuditResult]) -> set[str]:
    return {r.hotkey for r in results if r.verdict == AuditVerdict.PASSED}


def get_disqualified_hotkeys(results: list[AuditResult]) -> set[str]:
    return {r.hotkey for r in results if r.verdict == AuditVerdict.DISQUALIFIED}
