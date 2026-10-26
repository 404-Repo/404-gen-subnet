from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, TypeAdapter


if TYPE_CHECKING:
    from subnet_common.git_batcher import GitBatcher
    from subnet_common.github import GitHubClient


MAX_GENERATIONS_BYTES = 16 * 1024 * 1024


class GenerationResult(BaseModel):
    """One prompt's saved result.

    js is the JavaScript URL and views is the folder containing the canonical
    white/ and gray/ PNGs, grid.png and embeddings.npz. A failure carries a reason;
    render rejection can retain the JavaScript URL with no views.

    An empty result is a no-data sentinel and is not a completed outcome.
    """

    js: str | None = Field(default=None, description="CDN URL to the Three.js module")
    views: str | None = Field(default=None, description="CDN folder prefix containing white/ and gray/ rendered views")
    failure_reason: str | None = Field(
        default=None,
        description="Why this prompt has no usable result; None means a successful result or no-data sentinel",
    )
    size: int = Field(default=0, description="Size of the JS module in bytes")

    def is_failed(self) -> bool:
        """True when this stem has a recorded permanent delivery failure."""
        return self.failure_reason is not None


GenerationsMap = dict[str, GenerationResult]

GenerationsAdapter = TypeAdapter(GenerationsMap)


def _path(round_num: int, hotkey: str) -> str:
    filename = "generated_1.json" if hotkey == "leader" else "submitted.json"
    return f"rounds/{round_num}/{hotkey}/{filename}"


async def get_generations(
    git: GitHubClient,
    round_num: int,
    hotkey: str,
    ref: str,
) -> GenerationsMap:
    content = await git.get_file(
        path=_path(round_num=round_num, hotkey=hotkey),
        ref=ref,
    )
    if content is None:
        return {}
    return GenerationsAdapter.validate_json(content)


async def save_generations(
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    generations: GenerationsMap,
) -> None:
    path = _path(round_num=round_num, hotkey=hotkey)
    await git_batcher.write(
        path=path,
        content=GenerationsAdapter.dump_json(generations, indent=2).decode(),
        message=f"Update {path.rsplit('/', 1)[-1]} for round {round_num}, miner {hotkey[:10]}",
    )
