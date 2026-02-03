from pydantic import BaseModel, Field

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class RoundResult(BaseModel):
    """Final result of a competition round."""

    winner_hotkey: str = Field(description="'leader' if defended, miner hotkey if dethroned")
    repo: str = Field(description="GitHub repo of the winner")
    commit: str = Field(description="Git commit SHA of the winner")
    docker_image: str = Field(description="Docker image of the winner")


async def get_round_result(git: GitHubClient, round_num: int, ref: str) -> RoundResult | None:
    content = await git.get_file(f"rounds/{round_num}/winner.json", ref=ref)
    if content is None:
        return None
    return RoundResult.model_validate_json(content)


async def require_round_result(git: GitHubClient, round_num: int, ref: str) -> RoundResult:
    result = await get_round_result(git, round_num, ref)
    if result is None:
        raise FileNotFoundError(f"rounds/{round_num}/winner.json not found")
    return result


async def save_round_result(git_batcher: GitBatcher, round_num: int, result: RoundResult) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/winner.json",
        content=result.model_dump_json(indent=2),
        message=f"Round {round_num} winner: {result.winner_hotkey[:10]}",
    )
