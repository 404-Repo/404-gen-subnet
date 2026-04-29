from enum import StrEnum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationReportOutcome(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"


class RepeatStats(BaseModel):
    repeat_index: int
    generated_prompts: int = 0
    failed_prompts: int = 0
    generation_time: float | None = None


class GenerationReport(BaseModel):
    """Report of a miner's generation run, produced by the generation orchestrator.

    The orchestrator generates and renders; the judge service later runs a
    verification duel on reports with outcome COMPLETED.
    """

    hotkey: str
    outcome: GenerationReportOutcome = GenerationReportOutcome.PENDING
    repeats: list[RepeatStats] = Field(default_factory=list)
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
