from pydantic import BaseModel
from subnet_common.competition.generations import GenerationsMap, get_generations
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.schedule import require_schedule
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.submissions import MinerSubmission, require_submissions
from subnet_common.github import GitHubClient


class ValidationInputs(BaseModel):
    round_num: int
    hotkey: str
    ref: str
    submission: MinerSubmission
    prompts: list[str]
    seed: int
    generations: GenerationsMap
    witness_storage_prefix: str


async def load_validation_inputs(git: GitHubClient, round_num: int, hotkey: str, ref: str) -> ValidationInputs:
    """Read a candidate's inputs at its pinned competition state commit."""
    submissions = await require_submissions(git=git, round_num=round_num, ref=ref)
    submission = submissions[hotkey]
    prompts = await require_prompts(git=git, round_num=round_num, ref=ref)
    if not prompts:
        raise ValueError(f"No prompts for round {round_num}")
    seed = await require_seed_from_git(git=git, round_num=round_num, ref=ref)
    generations = await get_generations(git=git, round_num=round_num, hotkey=hotkey, ref=ref)
    if not generations:
        raise ValueError(f"No canonical outputs for round {round_num}, hotkey {hotkey}")
    schedule = await require_schedule(git=git, round_num=round_num, ref=ref)
    return ValidationInputs(
        round_num=round_num,
        hotkey=hotkey,
        ref=ref,
        submission=submission,
        prompts=prompts,
        seed=seed,
        generations=generations,
        witness_storage_prefix=schedule.witness_storage_prefix,
    )
