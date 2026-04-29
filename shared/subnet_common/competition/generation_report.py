from enum import StrEnum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationReportOutcome(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"


class GenerationReport(BaseModel):
    """Report of a miner's generation run, produced by the generation orchestrator.

    The orchestrator generates and renders; the judge service later runs a
    verification duel on reports with outcome COMPLETED.
    """

    hotkey: str
    outcome: GenerationReportOutcome = GenerationReportOutcome.PENDING
    checked_prompts: int = Field(default=0, description="Prompts regenerated so far")
    failed_prompts: int = Field(default=0, description="Prompts that failed to regenerate")
    generation_time: float | None = Field(default=None, description="Trimmed median generation time in seconds")
    reason: str = ""


GenerationReportsAdapter = TypeAdapter(dict[str, GenerationReport])


async def get_generation_reports(git: GitHubClient, round_num: int, ref: str) -> dict[str, GenerationReport]:
    content = await git.get_file(
        path=f"rounds/{round_num}/generation_reports.json",
        ref=ref,
    )
    if not content:
        return {}
    return GenerationReportsAdapter.validate_json(content)


async def save_generation_reports(
    git_batcher: GitBatcher, round_num: int, reports: dict[str, GenerationReport]
) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/generation_reports.json",
        content=GenerationReportsAdapter.dump_json(reports, indent=2).decode(),
        message=f"Update generation reports for round {round_num}",
    )


def get_completed_hotkeys(reports: dict[str, GenerationReport]) -> set[str]:
    return {hk for hk, r in reports.items() if r.outcome == GenerationReportOutcome.COMPLETED}


def get_rejected_hotkeys(reports: dict[str, GenerationReport]) -> set[str]:
    return {hk for hk, r in reports.items() if r.outcome == GenerationReportOutcome.REJECTED}
