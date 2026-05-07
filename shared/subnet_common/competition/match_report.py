from enum import Enum
from typing import Literal

from loguru import logger
from pydantic import BaseModel

from subnet_common.git_batcher import GitBatcher


MatchReportKind = Literal["duels", "audit"]


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


def _path(round_num: int, left: str, right: str, kind: MatchReportKind) -> str:
    """Path to a match report file.

    Stored under the challenger's (right) directory with reference to the defender (left).
    `kind` selects the filename prefix:
      - 'duels' — round-internal matches (qualification, timeline, exploratory).
      - 'audit' — post-hoc audit duels (submitted-vs-generated, generated-vs-defender).
    Two prefixes keeps audit artifacts visually separate from regular duels in the same dir.
    """
    return f"rounds/{round_num}/{right}/{kind}_{left[:10]}.json"


async def get_match_report(
    git_batcher: GitBatcher,
    round_num: int,
    left: str,
    right: str,
    *,
    kind: MatchReportKind = "duels",
) -> MatchReport | None:
    """Load match report from git (checks pending writes first). Returns None if not found or invalid."""
    path = _path(round_num, left, right, kind)
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
    *,
    kind: MatchReportKind = "duels",
) -> None:
    """Save a match report to git. Pass `kind='audit'` for audit-duel artifacts."""
    path = _path(round_num, report.left, report.right, kind)
    label = "Match report" if kind == "duels" else "Audit report"
    await git_batcher.write(
        path=path,
        content=report.model_dump_json(indent=2),
        message=f"{label}: {report.left[:10]} vs {report.right[:10]}",
    )
