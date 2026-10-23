from functools import cached_property
from typing import Literal

from pydantic import BaseModel, ConfigDict
from subnet_common.competition.match_matrix import MatchMatrix
from subnet_common.competition.source_audit import AuditResult, AuditVerdict
from subnet_common.competition.validation import ValidationResult, ValidationVerdict


EntryStatus = Literal["won", "lost", "pending", "rejected"]


class TimelineEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    hotkey: str
    status: EntryStatus
    defender: str
    margin: float | None = None
    verified: bool = False
    reason: str | None = None


class Timeline(BaseModel):
    model_config = ConfigDict(frozen=True, ignored_types=(cached_property,))

    entries: tuple[TimelineEntry, ...] = ()

    @cached_property
    def leaders(self) -> tuple[TimelineEntry, ...]:
        return tuple(e for e in self.entries if e.status == "won")

    @cached_property
    def next_match(self) -> tuple[str, str] | None:
        for e in self.entries:
            if e.status == "pending":
                return (e.defender, e.hotkey)
        return None

    def find_exploratory_duel(self, match_matrix: MatchMatrix) -> tuple[str, str] | None:
        candidates = [e.hotkey for e in self.entries if e.status != "rejected"]
        for i, left in enumerate(candidates):
            for right in candidates[i + 1 :]:
                if not match_matrix.has(left, right):
                    return left, right
        return None

    @cached_property
    def winner(self) -> str:
        return self.leaders[-1].hotkey if self.leaders else "leader"

    @cached_property
    def is_finalized(self) -> bool:
        for e in self.entries:
            if e.status == "pending":
                return False
            if e.status == "won" and not e.verified:
                return False
        return True

    @classmethod
    def derive(
        cls,
        all_hotkeys: list[str],
        match_matrix: MatchMatrix,
        win_margin: float,
        validation_results: dict[str, ValidationResult],
        source_audits: dict[str, AuditResult],
    ) -> "Timeline":
        """Build a Timeline from cached matches, validation results and source audits.

        `all_hotkeys` is sorted by `(revealed_at_block, tie_breaker)`.
        The judge uses a stored `tie_breaker`, or computes the BLAKE2b hex digest
        of `f"{seed}:{hotkey}".encode()` when that field is missing or null.

        Non-qualified miners (no cached leader match, or leader margin below
        win_margin) are silently omitted — they aren't part of the round.
        Qualified miners get one of:

        - rejected: a terminal validation or source-audit failure. A rejected
          hotkey does NOT advance the chain defender.
        - pending: qualified but has no cached match against the current chain
          defender yet.
        - won: beat the current chain defender by >= win_margin. Becomes the new
          chain defender. Verified iff its defender is verified and both its
          validation and source audit passed. Missing verdicts leave it unverified.
        - lost: a cached match against the current defender exists but didn't reach
          win_margin.

        Validation is independent of the defender. When a candidate fails, rebuild
        the timeline with remaining miners and reuse their validation results and
        cached matches. All leaders in the resulting chain must be verified before
        finalization.
        """
        entries: list[TimelineEntry] = []
        defender = "leader"
        defender_verified = True

        for hotkey in all_hotkeys:
            qualification = match_matrix.get("leader", hotkey)
            if qualification is None or qualification < win_margin:
                continue

            reason = candidate_rejection_reason(validation_results.get(hotkey), source_audits.get(hotkey))
            if reason is not None:
                entries.append(
                    TimelineEntry(
                        hotkey=hotkey,
                        status="rejected",
                        defender=defender,
                        reason=reason,
                    )
                )
                continue

            duel_margin = match_matrix.get(defender, hotkey)
            if duel_margin is None:
                entries.append(
                    TimelineEntry(
                        hotkey=hotkey,
                        status="pending",
                        defender=defender,
                    )
                )
                continue

            if duel_margin < win_margin:
                entries.append(
                    TimelineEntry(
                        hotkey=hotkey,
                        status="lost",
                        defender=defender,
                        margin=duel_margin,
                    )
                )
                continue

            audit = source_audits.get(hotkey)
            validation = validation_results.get(hotkey)
            verified = (
                defender_verified
                and audit is not None
                and audit.verdict == AuditVerdict.PASSED
                and validation is not None
                and validation.verdict == ValidationVerdict.PASSED
            )
            entries.append(
                TimelineEntry(
                    hotkey=hotkey,
                    status="won",
                    defender=defender,
                    margin=duel_margin,
                    verified=verified,
                )
            )
            defender = hotkey
            defender_verified = verified

        return cls(entries=tuple(entries))


def candidate_rejection_reason(validation: ValidationResult | None, source_audit: AuditResult | None) -> str | None:
    if validation is not None and validation.verdict == ValidationVerdict.FAILED:
        return f"validation: {validation.reason}"
    if source_audit is not None and source_audit.verdict == AuditVerdict.FAILED:
        return "source audit" + (f": {source_audit.reason[:64]}" if source_audit.reason else "")
    return None


def render_timeline(state: Timeline) -> str:
    if not state.entries:
        return "leader (no qualifiers)"
    lines = ["leader"]
    for i, e in enumerate(state.entries, start=1):
        hotkey = e.hotkey[:10]
        defender = e.defender[:10]
        if e.status == "won":
            mark = "verified" if e.verified else "unverified"
            lines.append(f"  {i}. {hotkey}  won {e.margin:+.2%} vs {defender}  [{mark}]")
        elif e.status == "pending":
            lines.append(f"  {i}. {hotkey}  pending")
        elif e.status == "lost":
            lines.append(f"  {i}. {hotkey}  lost to {defender} ({e.margin:+.2%})")
        elif e.status == "rejected":
            lines.append(f"  {i}. {hotkey}  rejected: {e.reason}")
    if state.leaders:
        lines.append(f"Current leader: {state.leaders[-1].hotkey[:10]}")
    return "\n".join(lines)
