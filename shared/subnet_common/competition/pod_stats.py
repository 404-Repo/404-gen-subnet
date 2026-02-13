from pydantic import BaseModel, Field, TypeAdapter

from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient


class PodStats(BaseModel):
    pod_id: str = Field(default="", description="Worker identifier (e.g. miner-01-abc1234def-0)")
    processed: int = Field(default=0, description="Total prompts attempted")
    successful: int = Field(default=0, description="Generations completed under the hard time limit")
    hard_failed: int = Field(default=0, description="Generations that produced no output (crash, HTTP error)")
    hard_overtime: int = Field(default=0, description="Generations that completed but exceeded the hard time limit")
    performance_samples: list[float] = Field(
        default_factory=list, description="Generation times from fresh (non-retry) prompts in seconds"
    )
    marked_bad: bool = Field(default=False, description="Whether the pod was marked bad and terminated early")
    retry_cumulative_delta: float = Field(default=0.0, description="Sum of (retry_time - original_time) across retries")
    retry_delta_count: int = Field(default=0, description="Number of retry deltas recorded")
    termination_reason: str = Field(
        default="", description="Why the pod stopped processing (e.g. 'no work available', 'stopped', 'mismatch limit')"
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
