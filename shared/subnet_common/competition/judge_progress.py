from pydantic import BaseModel, Field

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class JudgeProgress(BaseModel):
    """Tracks judging progress within a competition round."""

    leader: str = Field(
        default="leader", description="Hotkey of the current leader or `leader` for the previous round leader"
    )
    completed_hotkeys: list[str] = Field(
        default_factory=list, description="Hotkeys of challengers that have been judged"
    )


async def get_judge_progress(git: GitHubClient, current_round: int, ref: str) -> JudgeProgress:
    path = f"rounds/{current_round}/judge_progress.json"
    content = await git.get_file(path=path, ref=ref)
    if content is None:
        return JudgeProgress()
    return JudgeProgress.model_validate_json(content)


async def save_judge_progress(git_batcher: GitBatcher, current_round: int, progress: JudgeProgress) -> None:
    await git_batcher.write(
        path=f"rounds/{current_round}/judge_progress.json",
        content=progress.model_dump_json(indent=2),
        message=f"Update judge progress for round {current_round}",
    )
