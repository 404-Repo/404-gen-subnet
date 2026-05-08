from enum import StrEnum

from pydantic import BaseModel


class MatchOutcome(BaseModel):
    """Result of a match between two miners."""

    left: str
    right: str
    margin: float
    from_cache: bool = False


class VerificationOutcome(StrEnum):
    """Outcome of a verification check on a miner: PASSED, FAILED, or PENDING (undecided)."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
