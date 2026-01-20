from enum import Enum

from pydantic import BaseModel, Field

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class RoundStage(str, Enum):
    COLLECTING = "collecting"  # Gathering miner submissions for the current round
    GENERATING = "generating"  # Building containers, deploying models, generating 3D outputs, and rendering previews
    DUELS = "duels"  # Comparing generated outputs to determine the round winner
    FINALIZING = "finalizing"  # Updating the leader and creating the next round schedule
    FINISHED = "finished"  # Competition completes, no further rounds
    PAUSED = "paused"  # Manual hold for inspection or intervention (can occur after any stage)


class CompetitionState(BaseModel):
    """Competition state configuration."""

    current_round: int = Field(..., strict=False, description="Current round number")
    stage: RoundStage = Field(..., description="Current round stage")
    generation_deadline: int | None = Field(default=None, description="Generation stage deadline as UTC POSIX timestamp")


async def require_state(git: GitHubClient, ref: str) -> CompetitionState:
    content = await git.get_file("state.json", ref=ref)
    if content is None:
        raise FileNotFoundError("state.json not found")
    return CompetitionState.model_validate_json(content)


async def update_competition_state(git_batcher: GitBatcher, state: CompetitionState) -> None:
    await git_batcher.write(
        path="state.json",
        content=state.model_dump_json(indent=2),
        message=f"Update state ({state.stage.value}) for round {state.current_round}",
    )
