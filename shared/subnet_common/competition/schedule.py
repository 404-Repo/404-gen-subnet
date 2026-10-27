import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from subnet_common.github import GitHubClient


class RoundSchedule(BaseModel):
    """Competition round schedule configuration."""

    earliest_reveal_block: int
    latest_reveal_block: int
    generation_deadline_block: int | None = Field(..., ge=0)
    witness_storage_prefix: str = ""
    witness_save_submissions: bool = True

    @field_validator("witness_storage_prefix")
    @classmethod
    def storage_prefix(cls, value: str) -> str:
        if value and (len(value) > 200 or re.fullmatch(r"(?:[A-Za-z0-9_-]+/)+", value) is None):
            raise ValueError("Storage prefix must contain safe path segments and end in /")
        return value

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
