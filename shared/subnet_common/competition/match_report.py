from enum import Enum

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
    left_js: str | None = None
    left_png: str | None = None
    right_js: str | None = None
    right_png: str | None = None
    winner: DuelWinner = DuelWinner.SKIPPED
    detail: dict | None = None
    """Per-stage trace from the multi-stage judge: scores per VLM call + DINOv3 similarities.

    Free-form by design — each stage adds its own keys (s1/s2_bv/s2_ac/s2_cl/s3/s4).
    Only numeric/structural fields are kept; VLM `issues` text is dropped to bound size.
    """


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
    """Path to a match report file.

    Stored under the challenger's (right) directory with reference to the defender (left).
    """
    return f"rounds/{round_num}/{right}/duels_{left[:10]}.json"


async def save_match_report(
    git_batcher: GitBatcher,
    round_num: int,
    report: MatchReport,
) -> None:
    """Save a tournament match report to git."""
    path = _path(round_num, report.left, report.right)
    await git_batcher.write(
        path=path,
        content=report.model_dump_json(indent=2),
        message=f"Match report: {report.left[:10]} vs {report.right[:10]}",
    )
