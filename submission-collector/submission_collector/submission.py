import json

from loguru import logger
from pydantic import BaseModel, Field, ValidationError


class Submission(BaseModel):
    hotkey: str = Field(..., min_length=10, description="Bittensor hotkey of winner")
    reveal_block: int = Field(..., ge=0, description="Block number at which commit SHA was revealed")
    repo: str = Field(..., pattern=r"^[\w-]+/[\w-]+$", description="GitHub repo of winning solution")
    commit: str = Field(..., min_length=40, max_length=40, description="Git commit SHA")


def parse_commitment(
    hotkey: str,
    commitment: tuple[tuple[int, str], ...],
    earliest_block: int,
    latest_block: int,
) -> Submission | None:
    """Parse data from a revealed commitment."""

    valid_commits = [(block, data_str) for block, data_str in commitment if earliest_block <= block <= latest_block]

    if len(valid_commits) < 2:
        logger.debug(f"Insufficient commits for {hotkey}.")
        return None

    repo_commits = []
    commit_commits = []

    for block, data_str in valid_commits:
        try:
            data = json.loads(data_str.replace("'", '"'))

            if "repo" in data and data["repo"]:
                repo_commits.append((block, data["repo"]))

            if "commit" in data and data["commit"]:
                commit_commits.append((block, data["commit"]))

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.debug(f"Failed to parse data for {hotkey} at block {block}: {e}")
            continue

    if not repo_commits or not commit_commits:
        logger.debug(f"Missing repo or commit for {hotkey}")
        return None

    latest_repo_block, repo = max(repo_commits)
    latest_commit_block, commit = max(commit_commits)

    try:
        return Submission(hotkey=hotkey, reveal_block=latest_commit_block, repo=repo, commit=commit)
    except ValidationError as e:
        logger.debug(f"Invalid submission data for {hotkey}: {e}")
        return None
