from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class GenerationResult(BaseModel):
    """One miner's outcome for a single prompt stem.

    A persisted row is either:
    - Success: `js` is set (CDN URL to the Three.js module); `views` may or may not be
      set (None most likely means the JS itself was not renderable — counted as a
      delivery, but it won't win any matches).
    - Miner failure: `failure_reason` is set (from the miner's `_failed.json` manifest);
      `js` is None. Permanent — not retried.

    `views` is a CDN folder prefix. The judge constructs view URLs by convention:
    `{views}/white/{view_name}.png` for the 8 white-bg views and `{views}/gray/{view_name}.png`
    for the 4 gray-bg views. View names live in `subnet_common.render.WHITE_VIEWS / GRAY_VIEWS`.
    Atomic: either all 12 views were uploaded successfully, or `views` is None.

    An empty `GenerationResult()` is used as a "no data" sentinel by consumers and does
    not represent a persisted outcome.
    """

    js: str | None = Field(default=None, description="CDN URL to the Three.js module")
    views: str | None = Field(default=None, description="CDN folder prefix containing white/ and gray/ rendered views")
    failure_reason: str | None = Field(
        default=None,
        description=(
            "Why this stem has no usable output: a miner-reported failure or a collector "
            "pipeline failure (e.g. 'js_fetch_failed: HTTP 404', 'render_failed', "
            "'embeddings_failed'). None means a clean delivery or a no-data sentinel."
        ),
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
