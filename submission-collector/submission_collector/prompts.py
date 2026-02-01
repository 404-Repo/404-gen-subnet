import random

from loguru import logger
from subnet_common.competition.config import CompetitionConfig
from subnet_common.github import GitHubClient


async def select_prompts(
    git: GitHubClient,
    round_num: int,
    config: CompetitionConfig,
    seed: int,
    ref: str,
) -> list[str]:
    """Select prompts for the round: carryover from previous plus new from pool."""
    all_prompts = await _require_prompts(git, round_num=None, ref=ref)
    previous_prompts = [] if round_num == 0 else await _require_prompts(git, round_num - 1, ref=ref)

    rng = random.Random(seed)  # nosec B311 # noqa: S311

    if config.carryover_prompts > 0:
        carryover = _sample_up_to(rng, previous_prompts, config.carryover_prompts)
    else:
        carryover = []

    new_count = config.prompts_per_round - len(carryover)
    new = _sample_up_to(rng, all_prompts, new_count)

    logger.info(f"Selected {len(carryover)} carryover + {len(new)} new prompts")
    return carryover + new


async def _get_prompts(git: GitHubClient, round_num: int | None, ref: str) -> list[str] | None:
    """Fetch prompts for a round, or global pool if round_num is None."""
    path = "prompts.txt" if round_num is None else f"rounds/{round_num}/prompts.txt"
    content = await git.get_file(path, ref=ref)
    if content is None:
        return None
    return [line.strip() for line in content.splitlines() if line.strip()]


async def _require_prompts(git: GitHubClient, round_num: int | None, ref: str) -> list[str]:
    """Fetch prompts, raising if not found."""
    prompts = await _get_prompts(git, round_num, ref)
    if prompts is None:
        path = "prompts.txt" if round_num is None else f"rounds/{round_num}/prompts.txt"
        raise FileNotFoundError(f"{path} not found")
    return prompts


def _sample_up_to(rng: random.Random, items: list[str], k: int) -> list[str]:
    """Sample up to k items, returning all if fewer available."""
    if len(items) <= k:
        return items
    return rng.sample(items, k=k)
