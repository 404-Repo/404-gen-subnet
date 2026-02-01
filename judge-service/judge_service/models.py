from enum import Enum

from pydantic import BaseModel, Field


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


class MatchOutcome(BaseModel):
    """Result of a match between two miners."""

    left: str
    right: str
    margin: float
    decisive_prompts: list[str] = Field(default_factory=list)
    from_cache: bool = False
