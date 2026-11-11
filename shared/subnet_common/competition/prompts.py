from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from subnet_common.github import GitHubClient


def prompt_ids(urls: list[str]) -> list[str]:
    ids = [PurePosixPath(unquote(urlsplit(url).path)).stem for url in urls]
    if not ids or "" in ids or len(set(ids)) != len(ids):
        raise ValueError("Expected distinct official prompt IDs")
    return ids


async def require_prompts(git: GitHubClient, round_num: int | None, ref: str) -> list[str]:
    if round_num is None:
        path = "prompts.txt"
    else:
        path = f"rounds/{round_num}/prompts.txt"

    content = await git.get_file(path=path, ref=ref)
    if content is None:
        raise FileNotFoundError(f"{path} not found")

    prompts = [line for line in content.strip().split("\n") if line]

    return prompts
