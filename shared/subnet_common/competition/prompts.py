from subnet_common.github import GitHubClient


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
