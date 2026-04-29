from typing import Literal

from pydantic import BaseModel, ConfigDict

from judge_service.timeline import defender_audit_key, submitted_audit_key


class MatchOutcome(BaseModel):
    """Result of a match between two miners."""

    left: str
    right: str
    margin: float
    from_cache: bool = False


class AuditDuel(BaseModel):
    """One audit duel whose margin is missing from the audit matrix: which kind, for
    which repeat, against which defender. The right side is always the audited hotkey's
    generated outputs for `repeat_index`; `kind` picks the left side and the question:

      submitted — submitted vs generated_{r}: checks the submitted outputs aren't
          better than what the solution actually reproduces.
      defender — defender vs generated_{r}: verifies the solution genuinely beats
          the leader the miner beat.

    A RIGHT win is the legit signal for both kinds."""

    model_config = ConfigDict(frozen=True)

    hotkey: str
    defender: str
    repeat_index: int
    kind: Literal["submitted", "defender"]

    @property
    def left(self) -> str:
        """Left participant of the duel's match report: the 'submitted' sentinel or the defender."""
        if self.kind == "submitted":
            return "submitted"
        return self.defender

    @property
    def matrix_key(self) -> str:
        """Composite left key under which this duel's margin is recorded in the audit matrix."""
        if self.kind == "submitted":
            return submitted_audit_key(self.repeat_index)
        return defender_audit_key(self.defender, self.repeat_index)

    @property
    def log_label(self) -> str:
        return f"audit #{self.repeat_index} for {self.left[:10]} vs {self.hotkey[:10]}"
