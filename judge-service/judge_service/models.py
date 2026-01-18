from enum import Enum

from pydantic import BaseModel


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
    left_ply: str | None = None
    left_png: str | None = None
    right_ply: str | None = None
    right_png: str | None = None
    winner: DuelWinner = DuelWinner.SKIPPED
    issues: str = ""


class MatchReport(BaseModel):
    """Report for a full match between two miners."""

    left: str
    right: str
    score: int
    margin: float
    duels: list[DuelReport]
