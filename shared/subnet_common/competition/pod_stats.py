from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class PodStats(BaseModel):
    pod_id: str = Field(default="", description="Worker identifier (e.g. miner-01-abc1234def-0)")
    provider: str = Field(default="", description="GPU provider this pod was deployed on (e.g. 'targon', 'runpod')")
    batch_times: list[float] = Field(default_factory=list, description="Wall-clock time per batch in seconds")
    total_generation_time: float = Field(default=0.0, description="Total wall-clock generation time in seconds")
    termination_reason: str = Field(
        default="", description="Why the pod stopped processing (e.g. 'completed', 'crashed', 'replace_requested')"
    )
    payload: dict | None = Field(
        default=None, description="Diagnostic payload from the miner at termination time (e.g. health report)"
    )


PodStatsAdapter = TypeAdapter(dict[str, PodStats])


def _path(round_num: int, hotkey: str) -> str:
    return f"rounds/{round_num}/{hotkey}/pod_stats.json"


async def get_pod_stats(git: GitHubClient, round_num: int, hotkey: str, ref: str) -> dict[str, PodStats]:
    content = await git.get_file(
        path=_path(round_num=round_num, hotkey=hotkey),
        ref=ref,
    )
    if not content:
        return {}
    return PodStatsAdapter.validate_json(content)


async def save_pod_stats(
    git_batcher: GitBatcher,
    round_num: int,
    hotkey: str,
    stats: dict[str, PodStats],
) -> None:
    await git_batcher.write(
        path=_path(round_num=round_num, hotkey=hotkey),
        content=PodStatsAdapter.dump_json(stats, indent=2).decode(),
        message=f"Update pod stats for round {round_num}, miner {hotkey[:10]}",
    )
