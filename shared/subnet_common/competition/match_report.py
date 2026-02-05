from enum import Enum

from loguru import logger
from pydantic import BaseModel

from subnet_common.git_batcher import GitBatcher


class DuelWinner(str, Enum):
    """Winner of a single prompt duel."""

    LEFT = "left"
    RIGHT = "right"
    DRAW = "draw"
    SKIPPED = "skipped"


class DuelReport(BaseModel):
    """Report for a single prompt duel."""

    name: str
    prompt: str
    left_glb: str | None = None
    left_png: str | None = None
    right_glb: str | None = None
    right_png: str | None = None
    winner: DuelWinner = DuelWinner.SKIPPED
    issues: str = ""


class MatchReport(BaseModel):
    """
    Detailed match report, stored in git.
    Contains all duel results for transparency and auditability.
    """

    left: str
    right: str
    score: int
    margin: float
    duels: list[DuelReport]


def _path(round_num: int, left: str, right: str) -> str:
    """Path to match a report file.
    Convention: stored under the challenger's (right) directory with reference to the defender (left).
    """
    return f"rounds/{round_num}/{right}/duels_{left[:10]}.json"


async def get_match_report(
    git_batcher: GitBatcher,
    round_num: int,
    left: str,
    right: str,
) -> MatchReport | None:
    """Load match report from git (checks pending writes first). Returns None if not found or invalid."""
    path = _path(round_num, left, right)
    try:
        content = await git_batcher.read(path)
        if not content:
            return None
        return MatchReport.model_validate_json(content)
    except Exception as e:
        logger.warning(f"Failed to load match report {path}: {e}")
        return None


async def save_match_report(
    git_batcher: GitBatcher,
    round_num: int,
    report: MatchReport,
) -> None:
    """Save a match report to git."""
    path = _path(round_num, report.left, report.right)
    await git_batcher.write(
        path=path,
        content=report.model_dump_json(indent=2),
        message=f"Match report: {report.left[:10]} vs {report.right[:10]}",
    )
