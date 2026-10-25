from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.github import GitHubClient


class MinerSubmission(BaseModel):
    """Immutable Python source submission info for a round."""

    repo: str = Field(..., description="GitHub repository in format 'owner/repo'")
    commit: str = Field(..., description="Git commit SHA")
    revealed_at_block: int = Field(..., description="Block number when miner's submission was revealed")
    tie_breaker: str = Field(
        ...,
        pattern=r"^[0-9a-f]{128}$",
        description="Seed-based BLAKE2b hex digest for ordering reveals in the same block",
    )
    round: str = Field(..., description="Full round name: competition name and round number")


async def require_submissions(git: GitHubClient, round_num: int, ref: str) -> dict[str, MinerSubmission]:
    path = f"rounds/{round_num}/submissions.json"
    content = await git.get_file(path=path, ref=ref)
    if content is None:
        raise FileNotFoundError(f"{path} not found")
    adapter = TypeAdapter(dict[str, MinerSubmission])
    return adapter.validate_json(content)
