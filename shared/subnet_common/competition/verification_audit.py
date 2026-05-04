"""Verification audit verdicts produced by judge-service.

After the generation orchestrator finishes a re-run for an audited hotkey, the judge
runs a duel between the miner's submitted output and our re-generated output. The sum
of per-prompt outcomes (-1 if submitted dominates, +1 if generated holds up, 0 for
draws / skips) determines the verdict: sum >= 0 → PASSED; otherwise FAILED. A draw is
acceptable — the miner only loses verification when submitted disproportionately beats
generated, which is the cheating signal.
"""

from enum import StrEnum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class VerificationOutcome(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class VerificationAudit(BaseModel):
    """One verification verdict for a hotkey within a round.

    Per-duel outcome breakdown so readers don't have to load the full duels file
    to understand the verdict shape:
        total_prompts = checked_prompts + drawn
    """

    hotkey: str
    outcome: VerificationOutcome = VerificationOutcome.PENDING
    score: int = Field(default=0, description="Sum of per-prompt outcomes: -1 submitted, +1 generated, 0 otherwise")
    total_prompts: int = Field(default=0, description="Total duels in the audit")
    checked_prompts: int = Field(
        default=0,
        description="Duels the judge ran to a verdict (decisive or draw); "
        "equals total_prompts when the judge didn't crash",
    )
    drawn: int = Field(
        default=0, description="Duels the multi-stage judge ruled a draw (sides equivalent or both missing previews)"
    )
    reason: str = ""


VerificationAuditsAdapter = TypeAdapter(dict[str, VerificationAudit])


def _path(round_num: int) -> str:
    return f"rounds/{round_num}/verification_audits.json"


async def get_verification_audits(git: GitHubClient, round_num: int, ref: str) -> dict[str, VerificationAudit]:
    content = await git.get_file(path=_path(round_num), ref=ref)
    if not content:
        return {}
    return VerificationAuditsAdapter.validate_json(content)


async def save_verification_audits(
    git_batcher: GitBatcher, round_num: int, audits: dict[str, VerificationAudit]
) -> None:
    await git_batcher.write(
        path=_path(round_num),
        content=VerificationAuditsAdapter.dump_json(audits, indent=2).decode(),
        message=f"Update verification audits for round {round_num}",
    )


def get_passed_hotkeys(audits: dict[str, VerificationAudit]) -> set[str]:
    return {hk for hk, a in audits.items() if a.outcome == VerificationOutcome.PASSED}


def get_failed_hotkeys(audits: dict[str, VerificationAudit]) -> set[str]:
    return {hk for hk, a in audits.items() if a.outcome == VerificationOutcome.FAILED}
