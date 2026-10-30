from pydantic import BaseModel


class MatchOutcome(BaseModel):
    """Result of a match between two miners."""

    left: str
    right: str
    margin: float
    from_cache: bool = False
