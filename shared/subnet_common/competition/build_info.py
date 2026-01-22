from enum import Enum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.competition.submissions import MinerSubmission
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class BuildStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    TIMED_OUT = "timed_out"
    NOT_FOUND = "not_found"


class BuildInfo(BaseModel):
    repo: str = Field(..., description="GitHub repository in format 'owner/repo'")
    commit: str = Field(..., description="Git commit SHA")
    revealed_at_block: int = Field(..., description="Block number when miner's submission was revealed")
    tag: str = Field(..., description="Docker image tag")
    status: BuildStatus = Field(default=BuildStatus.PENDING, description="Build status")
    docker_image: str | None = Field(default=None, description="Docker image URL")

    @classmethod
    def from_submission(cls, submission: MinerSubmission) -> "BuildInfo":
        return BuildInfo(
            repo=submission.repo,
            commit=submission.commit,
            revealed_at_block=submission.revealed_at_block,
            tag=f"{submission.commit[:7]}-{submission.round}",
        )

    def get_runnable_image(self) -> str | None:
        """Return docker_image if build succeeded and image exists, None otherwise."""
        if self.status == BuildStatus.SUCCESS and self.docker_image is not None:
            return self.docker_image
        return None


BuildsInfoAdapter = TypeAdapter(dict[str, BuildInfo])


async def require_builds(git: GitHubClient, round_num: int, ref: str) -> dict[str, BuildInfo]:
    path = f"rounds/{round_num}/builds.json"
    builds = await get_builds(git, round_num, ref)
    if builds is None:
        raise FileNotFoundError(f"{path} not found")
    return builds


async def get_builds(git: GitHubClient, round_num: int, ref: str) -> dict[str, BuildInfo] | None:
    path = f"rounds/{round_num}/builds.json"
    content = await git.get_file(path=path, ref=ref)
    if content is None:
        return None
    return BuildsInfoAdapter.validate_json(content)


async def save_builds(git_batcher: GitBatcher, current_round: int, builds: dict[str, BuildInfo]) -> None:
    content = BuildsInfoAdapter.dump_json(builds, indent=2).decode()
    await git_batcher.write(
        path=f"rounds/{current_round}/builds.json",
        content=content,
        message=f"Update build statuses for round {current_round}",
    )
