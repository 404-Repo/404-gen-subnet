from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class RoundStage(str, Enum):
    OPEN = "open"  # Submission window opens, miners register their submissions
    MINER_GENERATION = "miner_generation"  # Seed published, miners have 3h to generate and upload
    DOWNLOADING = "downloading"  # Fetching 3D from miner CDNs
    DUELS = "duels"  # Duels, verification, and approval
    FINALIZING = "finalizing"  # Updating the leader and creating the next round schedule
    FINISHED = "finished"  # Competition completes, no further rounds
    PAUSED = "paused"  # Manual hold for inspection or intervention


class CompetitionState(BaseModel):
    """Competition state configuration."""

    current_round: int = Field(..., strict=False, description="Current round number")
    stage: RoundStage = Field(..., description="Current round stage")
    next_stage_eta: datetime | None = Field(
        default=None,
        description="Estimated UTC time when the next stage begins (approximate)",
    )


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
