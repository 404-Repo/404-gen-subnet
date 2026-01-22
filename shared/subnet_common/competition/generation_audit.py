from enum import StrEnum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationAuditOutcome(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    REJECTED = "rejected"


class GenerationAudit(BaseModel):
    """Audit result for a miner's generation capabilities."""

    hotkey: str
    outcome: GenerationAuditOutcome = GenerationAuditOutcome.PENDING
    checked_prompts: int = Field(default=0, description="Prompts regenerated so far")
    failed_prompts: int = Field(default=0, description="Non-critical prompts that failed")
    failed_critical: int = Field(default=0, description="Critical prompts that failed")
    generation_time: float | None = Field(default=None, description="Trimmed median generation time in seconds")
    reason: str = ""


GenerationAuditsAdapter = TypeAdapter(dict[str, GenerationAudit])


async def get_generation_audits(git: GitHubClient, round_num: int, ref: str) -> dict[str, GenerationAudit]:
    content = await git.get_file(
        path=f"rounds/{round_num}/generation_audits.json",
        ref=ref,
    )
    if not content:
        return {}
    return GenerationAuditsAdapter.validate_json(content)


async def save_generation_audits(git_batcher: GitBatcher, round_num: int, audits: dict[str, GenerationAudit]) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/generation_audits.json",
        content=GenerationAuditsAdapter.dump_json(audits, indent=2).decode(),
        message=f"Update generation audits for round {round_num}",
    )


def get_passed_hotkeys(audits: dict[str, GenerationAudit]) -> set[str]:
    return {hk for hk, a in audits.items() if a.outcome == GenerationAuditOutcome.PASSED}


def get_rejected_hotkeys(audits: dict[str, GenerationAudit]) -> set[str]:
    return {hk for hk, a in audits.items() if a.outcome == GenerationAuditOutcome.REJECTED}


def needs_audit(audits: dict[str, GenerationAudit], hotkey: str) -> bool:
    a = audits.get(hotkey)
    return a is None or a.outcome == GenerationAuditOutcome.PENDING
