from functools import cached_property
from typing import Literal

from pydantic import BaseModel, ConfigDict
from subnet_common.competition.audit_requests import AuditRequests
from subnet_common.competition.generation_report import GenerationReport, GenerationReportOutcome
from subnet_common.competition.match_matrix import MatchMatrix
from subnet_common.competition.source_audit import AuditResult, AuditVerdict


def submitted_audit_key(repeat_index: int) -> str:
    return f"submitted_{repeat_index}"


def defender_audit_key(defender: str, repeat_index: int) -> str:
    return f"{defender}_{repeat_index}"


EntryStatus = Literal["won", "lost", "pending", "rejected"]


class TimelineEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    hotkey: str
    status: EntryStatus
    defender: str | None = None
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
                return (e.defender or "leader", e.hotkey)
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
        audit_matrix: MatchMatrix,
        audit_repeats: int,
        audit_requests: AuditRequests,
        generation_reports: dict[str, GenerationReport],
        source_audits: dict[str, AuditResult],
    ) -> "Timeline":
        """Build a Timeline from cached match results and audit verdicts.

        Precondition: `all_hotkeys` is sorted by reveal time.

        Non-qualified miners (no cached leader match, or leader margin below
        win_margin) are silently omitted — they aren't part of the round.
        Qualified miners get one of:

        - rejected: disqualified by a rejected generation report, a failed source
          audit, or an audit-margin failure (see `_rejection_reason`). A rejected
          hotkey does NOT advance the chain defender — subsequent hotkeys are still
          tested against the previous defender.
        - pending: qualified but has no cached match against the current chain
          defender yet.
        - won: beat the current chain defender by >= win_margin. Becomes the new
          chain defender. Marked verified iff its defender was verified, the hotkey
          has a PASSED source audit, and all per-repeat audits pass.
        - lost: a cached match against the current defender exists but didn't reach
          win_margin.
        """
        entries: list[TimelineEntry] = []
        defender = "leader"
        defender_verified = True

        for hotkey in all_hotkeys:
            qualification = match_matrix.get("leader", hotkey)
            if qualification is None or qualification < win_margin:
                continue

            reason = (
                _rejection_from_generation_report(generation_reports.get(hotkey))
                or _rejection_from_source_audit(source_audits.get(hotkey))
                or _rejection_from_submitted_audit(hotkey, audit_matrix, audit_repeats, win_margin)
            )
            if reason is None and defender_verified:
                request = audit_requests.get(hotkey)
                if request is not None:
                    reason = _rejection_from_defender_audit(
                        hotkey, request.latest_defender, audit_matrix, audit_repeats, win_margin
                    )
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
            verified = (
                defender_verified
                and audit is not None
                and audit.verdict == AuditVerdict.PASSED
                and _all_duel_audits_pass(hotkey, defender, audit_matrix, audit_repeats, win_margin)
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


def _all_duel_audits_pass(
    hotkey: str,
    defender: str,
    audit_matrix: MatchMatrix,
    audit_repeats: int,
    win_margin: float,
) -> bool:
    for r in range(1, audit_repeats + 1):
        submitted_margin = audit_matrix.get(submitted_audit_key(r), hotkey)
        defender_margin = audit_matrix.get(defender_audit_key(defender, r), hotkey)
        if (
            submitted_margin is None
            or submitted_margin < -win_margin
            or defender_margin is None
            or defender_margin < win_margin
        ):
            return False
    return True


def _rejection_from_generation_report(report: GenerationReport | None) -> str | None:
    if report is None or report.outcome != GenerationReportOutcome.REJECTED:
        return None
    return "generation report" + (f": {report.reason}" if report.reason else "")


def _rejection_from_source_audit(audit: AuditResult | None) -> str | None:
    if audit is None or audit.verdict != AuditVerdict.FAILED:
        return None
    return "source audit" + (f": {audit.reason}" if audit.reason else "")


def _rejection_from_submitted_audit(
    hotkey: str, audit_matrix: MatchMatrix, audit_repeats: int, win_margin: float
) -> str | None:
    for r in range(1, audit_repeats + 1):
        margin = audit_matrix.get(submitted_audit_key(r), hotkey)
        if margin is not None and margin < -win_margin:
            return f"submitted vs generated #{r}: {margin:+.2%}"
    return None


def _rejection_from_defender_audit(
    hotkey: str, defender: str, audit_matrix: MatchMatrix, audit_repeats: int, win_margin: float
) -> str | None:
    for r in range(1, audit_repeats + 1):
        margin = audit_matrix.get(defender_audit_key(defender, r), hotkey)
        if margin is not None and margin < win_margin:
            return f"{defender[:10]} vs generated #{r}: {margin:+.2%}"
    return None


def render_timeline(state: Timeline) -> str:
    if not state.entries:
        return "leader (no qualifiers)"
    lines = ["leader"]
    for i, e in enumerate(state.entries, start=1):
        hotkey = e.hotkey[:10]
        defender = (e.defender or "leader")[:10]
        if e.status == "won":
            mark = "verified" if e.verified else "unverified"
            lines.append(f"  {i}. {hotkey}  won {e.margin:+.2%} vs {defender}  [{mark}]")
        elif e.status == "pending":
            lines.append(f"  {i}. {hotkey}  pending")
        elif e.status == "lost":
            lines.append(f"  {i}. {hotkey}  lost to {defender} ({e.margin:+.2%})")
        elif e.status == "rejected":
            lines.append(f"  {i}. {hotkey}  rejected: {e.reason}")
    winners = [e for e in state.entries if e.status == "won"]
    if winners:
        lines.append(f"current leader: {winners[-1].hotkey[:10]}")
    return "\n".join(lines)
