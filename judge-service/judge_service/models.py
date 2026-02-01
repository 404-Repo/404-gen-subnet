from pydantic import BaseModel, Field


class MatchOutcome(BaseModel):
    """Result of a match between two miners."""

    left: str
    right: str
    margin: float
    decisive_prompts: list[str] = Field(default_factory=list)
    from_cache: bool = False
