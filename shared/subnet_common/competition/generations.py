from enum import StrEnum

from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationSource(StrEnum):
    SUBMITTED = "submitted"
    GENERATED = "generated"


class GenerationResult(BaseModel):
    glb: str | None = Field(default=None, description="CDN URL to GLB file")
    png: str | None = Field(default=None, description="CDN URL to rendered preview")
    generation_time: float = Field(default=0, description="Time taken to generate the result in seconds")
    size: int = Field(default=0, description="Size of the GLB file in bytes")
    attempts: int = Field(default=0, description="Number of generation attempts (persisted for restart continuity)")
    distance: float = Field(
        default=0.0, description="Cosine distance between submitted and generated results using DINOv3 embeddings"
    )

    def is_failed(self) -> bool:
        return self.glb is None or self.png is None

    def is_overtime(self, timeout: float) -> bool:
        return self.generation_time > timeout

    def needs_retry(self, timeout: float, max_attempts: int | None = None) -> bool:
        """Check if generation needs retry.

        Returns True if generation failed or exceeded timeout,
        AND has attempts remaining (if max_attempts specified).
        """
        if not self.is_failed() and not self.is_overtime(timeout):
            return False
        if max_attempts is not None and self.attempts >= max_attempts:
            return False
        return True


GenerationsAdapter = TypeAdapter(dict[str, GenerationResult])


def _path(round_num: int, hotkey: str, source: GenerationSource) -> str:
    return f"rounds/{round_num}/{hotkey}/{source}.json"


async def get_generations(
    git: GitHubClient, round_num: int, hotkey: str, source: GenerationSource, ref: str
) -> dict[str, GenerationResult]:
    content = await git.get_file(
        path=_path(round_num=round_num, hotkey=hotkey, source=source),
        ref=ref,
    )
    if not content:
        return {}
    return GenerationsAdapter.validate_json(content)


async def save_generations(
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    source: GenerationSource,
    generations: dict[str, GenerationResult],
) -> None:
    await git_batcher.write(
        path=_path(round_num=round_num, hotkey=hotkey, source=source),
        content=GenerationsAdapter.dump_json(generations, indent=2).decode(),
        message=f"Update {source} for round {round_num}, miner {hotkey[:10]}",
    )
