from dataclasses import dataclass

from loguru import logger


@dataclass(frozen=True)
class ValidationJob:
    round_num: int
    hotkey: str
    competition_state_commit: str


async def execute_validation_job(job: ValidationJob) -> None:
    """Log a ready job, leaving validation pending."""
    logger.info(
        "Validation job ready: round={}, hotkey={}, competition_state_commit={}",
        job.round_num,
        job.hotkey,
        job.competition_state_commit,
    )
