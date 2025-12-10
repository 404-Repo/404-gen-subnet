import json

from subnet_common.github import GitHubClient


async def get_seed_from_git(git: GitHubClient, round_num: int, ref: str) -> int | None:
    """Load seed from Git, returning None if not found."""
    content = await git.get_file(f"rounds/{round_num}/seed.json", ref=ref)
    if content is None:
        return None
    data = json.loads(content)
    return int(data["seed"])


async def require_seed_from_git(git: GitHubClient, round_num: int, ref: str) -> int:
    seed = await get_seed_from_git(git, round_num, ref=ref)
    if seed is None:
        raise FileNotFoundError(f"Seed file not found for round {round_num}")
    return seed
