from pydantic import BaseModel, ConfigDict

from subnet_common.github import GitHubClient


class RoundSchedule(BaseModel):
    """Competition round schedule configuration."""

    earliest_reveal_block: int
    latest_reveal_block: int

    model_config = ConfigDict(populate_by_name=True)


async def require_schedule(git: GitHubClient, round_num: int, ref: str) -> RoundSchedule:
    schedule = await get_schedule(git, round_num, ref)
    if schedule is None:
        raise FileNotFoundError(f"rounds/{round_num}/schedule.json not found")
    return schedule


async def get_schedule(git: GitHubClient, round_num: int, ref: str) -> RoundSchedule | None:
    path = f"rounds/{round_num}/schedule.json"
    content = await git.get_file(path=path, ref=ref)
    if content is None:
        return None
    return RoundSchedule.model_validate_json(content)
