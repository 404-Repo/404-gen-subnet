from enum import StrEnum

from pydantic import BaseModel, Field


class DuelWinner(StrEnum):
    LEADER = "leader"
    MINER = "miner"
    DRAW = "draw"
    SKIPPED = "skipped"


class DuelReport(BaseModel):
    """Result of a single duel between the current leader and a challenger."""

    name: str = Field(..., description="Stem of the prompt file (identifier)")
    prompt: str = Field(..., description="URL to the prompt file")
    leader_ply: str | None = Field(default=None, description="URL to the leader's generated PLY model")
    leader_png: str | None = Field(default=None, description="URL to the leader's rendered PNG preview")
    miner_ply: str | None = Field(default=None, description="URL to the miner's generated PLY model")
    miner_png: str | None = Field(default=None, description="URL to the miner's rendered PNG preview")
    winner: DuelWinner = Field(default=DuelWinner.SKIPPED, description="Duel outcome")
    issues: str = Field(default="", description="Generation issues detected by the judge LLM")


class MatchOutcome(StrEnum):
    LEADER_DEFENDS = "leader_defends"
    CHALLENGER_WINS = "challenger_wins"


class MatchReport(BaseModel):
    """Result of a full match (multiple duels) between a leader and challenger."""

    leader: str = Field(..., description="Hotkey of the current leader")
    hotkey: str = Field(..., description="Hotkey of the challenger/contender")
    score: int = Field(default=0, description="Overall score: +1 per win, -1 per loss, 0 per draw")
    margin: float = Field(default=0.0, description="Normalized margin: score / len(duels)")
    outcome: MatchOutcome = Field(default=MatchOutcome.LEADER_DEFENDS, description="Match result")
    duels: list[DuelReport] = Field(default_factory=list, description="Individual duel results")
