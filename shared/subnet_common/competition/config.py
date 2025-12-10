from datetime import date, time

from pydantic import BaseModel, Field

from subnet_common.github import GitHubClient


class CompetitionConfig(BaseModel):
    """Competition configuration and schedule."""

    name: str = Field(..., description="Competition name")
    description: str = Field(..., description="Competition description")
    first_evaluation_date: date = Field(..., description="First day of evaluation rounds")
    last_competition_date: date = Field(description="Last allowed day for round start")
    round_start_time: time = Field(default=time(12, 0), description="Daily round start time (UTC)")
    finalization_buffer_hours: float = Field(
        default=1.0, description="Skip day if FINALIZING with less than this remaining"
    )
    win_margin: float = Field(..., description="Challenger must win by this fraction to dethrone leader")
    weight_decay: float = Field(..., description="Weight reduction per round if leader not defeated")
    weight_floor: float = Field(..., description="Minimum leader weight")
    prompts_per_round: int = Field(..., description="Number of prompts to select for each round")
    carryover_prompts: int = Field(..., description="Number of prompts retained from the previous round")


async def require_competition_config(git: GitHubClient, ref: str) -> CompetitionConfig:
    path = "config.json"
    content = await git.get_file(path=path, ref=ref)
    if content is None:
        raise FileNotFoundError(f"{path} not found")
    return CompetitionConfig.model_validate_json(content)
