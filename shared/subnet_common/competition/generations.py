from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationResult(BaseModel):
    ply: str | None = Field(default=None, description="CDN URL to PLY file")
    png: str | None = Field(default=None, description="CDN URL to rendered preview")
    generation_time: float = 0
    size: int = 0


MinerGenerationsAdapter = TypeAdapter(dict[str, GenerationResult])


async def get_miner_generations(
    git: GitHubClient, hotkey: str, round_num: int, ref: str
) -> dict[str, GenerationResult]:
    content = await git.get_file(f"rounds/{round_num}/{hotkey}/generations.json", ref=ref)
    if not content:
        return {}
    return MinerGenerationsAdapter.validate_json(content)


async def save_miner_generations(
    git_batcher: GitBatcher, hotkey: str, round_num: int, generations: dict[str, GenerationResult]
) -> None:
    await git_batcher.write(
        path=f"rounds/{round_num}/{hotkey}/generations.json",
        content=MinerGenerationsAdapter.dump_json(generations, indent=2).decode(),
        message=f"Update generations for round {round_num}, miner {hotkey[:10]}",
    )
