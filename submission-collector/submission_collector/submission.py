import json

from loguru import logger
from pydantic import BaseModel, Field, HttpUrl, ValidationError


class Submission(BaseModel):
    hotkey: str = Field(..., min_length=10, description="Bittensor hotkey of winner")
    reveal_block: int = Field(..., ge=0, description="Block number at which commit SHA was revealed")
    repo: str = Field(..., pattern=r"^[\w-]+/[\w-]+$", description="GitHub repo of winning solution")
    commit: str = Field(..., min_length=40, max_length=40, description="Git commit SHA")
    cdn_url: HttpUrl = Field(..., description="CDN URL of the directory with generated GLB files")


def parse_commitment(
    hotkey: str,
    commitment: tuple[tuple[int, str], ...],
    earliest_block: int,
    latest_block: int,
) -> Submission | None:
    """Parse data from a revealed commitment.

    Miners can submit fields across multiple commits within the window.
    The latest value for each field (repo, commit, cdn_url) is used.
    """
    valid_commits = [(block, data_str) for block, data_str in commitment if earliest_block <= block <= latest_block]

    if not valid_commits:
        logger.debug(f"No commits in window for {hotkey}")
        return None

    repo_commits = []
    commit_commits = []
    cdn_url_commits = []

    for block, data_str in valid_commits:
        try:
            data = json.loads(data_str.replace("'", '"'))

            if "repo" in data and data["repo"]:
                repo_commits.append((block, data["repo"]))

            if "commit" in data and data["commit"]:
                commit_commits.append((block, data["commit"]))

            if "cdn_url" in data and data["cdn_url"]:
                cdn_url_commits.append((block, data["cdn_url"]))

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.debug(f"Failed to parse data for {hotkey} at block {block}: {e}")
            continue

    if not all([repo_commits, commit_commits, cdn_url_commits]):
        logger.debug(f"Missing repo, commit, or cdn_url for {hotkey}")
        return None

    _, repo = max(repo_commits)
    latest_commit_block, commit = max(commit_commits)
    _, cdn_url = max(cdn_url_commits)
    cdn_url = cdn_url.rstrip("/")

    try:
        return Submission(hotkey=hotkey, reveal_block=latest_commit_block, repo=repo, commit=commit, cdn_url=cdn_url)
    except ValidationError as e:
        logger.debug(f"Invalid submission data for {hotkey}: {e}")
        return None
