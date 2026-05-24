"""Spec tests for Timeline.derive and its companion display helpers.

Written from the *expected* semantics, not from the implementation. They should
pass if Timeline.derive correctly models:

- "qualified" = the miner beat the base leader in their qualification match
- chain = sequence of promoted miners, each beating the previous chain head
- losers = miners who fought and lost a timeline match (still in the round)
- pending = qualified miners not yet matched against the current chain head
- rejected = miners disqualified by audit/generation verdicts
- never-qualified miners are silently omitted from the state
- a rejected chain member does NOT advance the defender — subsequent miners
  challenge the previous defender, so the chain rebuilds from cached matches
- verification cascades: chain member is verified only if all prior chain members are
"""

import pytest
from subnet_common.competition.audit_requests import AuditRequest, AuditRequests
from subnet_common.competition.generation_report import GenerationReport, GenerationReportOutcome
from subnet_common.competition.match_matrix import MatchMatrix
from subnet_common.competition.source_audit import AuditResult, AuditVerdict

from judge_service.timeline import (
    Timeline,
    TimelineEntry,
    defender_audit_key,
    render_timeline,
    submitted_audit_key,
)


WIN_MARGIN = 0.1
A = "0xaaaaaaaaaaaa"
B = "0xbbbbbbbbbbbb"
C = "0xcccccccccccc"
D = "0xdddddddddddd"
LEADER = "leader"


def _derive(
    *,
    all_hotkeys: list[str] | None = None,
    match_matrix: MatchMatrix | None = None,
    win_margin: float = WIN_MARGIN,
    audit_matrix: MatchMatrix | None = None,
    audit_repeats: int = 1,
    audit_requests: AuditRequests | None = None,
    generation_rejected: set[str] | None = None,
    source_audit_passed: set[str] | None = None,
    source_audit_failed: set[str] | None = None,
) -> Timeline:
    reports = {
        hk: GenerationReport(hotkey=hk, outcome=GenerationReportOutcome.REJECTED)
        for hk in (generation_rejected or set())
    }
    audits = {
        **{hk: AuditResult(hotkey=hk, verdict=AuditVerdict.PASSED) for hk in (source_audit_passed or set())},
        **{hk: AuditResult(hotkey=hk, verdict=AuditVerdict.FAILED) for hk in (source_audit_failed or set())},
    }
    return Timeline.derive(
        all_hotkeys=all_hotkeys or [],
        match_matrix=match_matrix or MatchMatrix(),
        win_margin=win_margin,
        audit_matrix=audit_matrix or MatchMatrix(),
        audit_repeats=audit_repeats,
        audit_requests=audit_requests or AuditRequests(),
        generation_reports=reports,
        source_audits=audits,
    )


def _passing_audit_matrix(*pairs: tuple[str, str], audit_repeats: int = 1) -> MatchMatrix:
    """For each (hotkey, defender) pair, write submitted_r and {defender}_r margins
    above thresholds for every repeat. So `(hk, def)` records that hk's audit duels
    pass vs def for every r in 1..audit_repeats."""
    m = MatchMatrix()
    for hotkey, defender in pairs:
        for r in range(1, audit_repeats + 1):
            m.add(submitted_audit_key(r), hotkey, 0.5)
            m.add(defender_audit_key(defender, r), hotkey, 0.5)
    return m


def _entry(state: Timeline, hotkey: str) -> TimelineEntry | None:
    for e in state.entries:
        if e.hotkey == hotkey:
            return e
    return None


def test_no_hotkeys_produces_no_entries() -> None:
    state = _derive()
    assert state.entries == ()


def test_no_hotkeys_is_finalized_to_leader() -> None:
    state = _derive()
    assert state.winner == LEADER
    assert state.is_finalized is True
    assert state.next_match is None
    assert state.leaders == ()


def test_hotkey_without_leader_match_is_omitted() -> None:
    # X has no leader match cached → never qualified → not in entries.
    state = _derive(all_hotkeys=[A], match_matrix=MatchMatrix())
    assert state.entries == ()


def test_hotkey_lost_qualification_is_omitted() -> None:
    # A fought leader, lost (margin < win_margin) → never qualified → omitted.
    mm = MatchMatrix()
    mm.add(LEADER, A, -0.5)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.entries == ()


def test_single_qualifier_is_promoted_vs_leader() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert len(state.entries) == 1
    entry = state.entries[0]
    assert entry.hotkey == A
    assert entry.status == "won"
    assert entry.defender == LEADER
    assert entry.margin == pytest.approx(0.3)


def test_promoted_is_unverified_without_audit_data() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.entries[0].verified is False


def test_promoted_is_verified_when_audits_and_source_audit_pass() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = _passing_audit_matrix((A, LEADER))
    state = _derive(
        all_hotkeys=[A],
        match_matrix=mm,
        audit_matrix=am,
        source_audit_passed={A},
    )
    assert state.entries[0].verified is True


def test_single_qualifier_winner_is_them() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.winner == A


def test_unverified_single_qualifier_is_not_finalized() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.is_finalized is False


def test_verified_single_qualifier_is_finalized() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = _passing_audit_matrix((A, LEADER))
    state = _derive(
        all_hotkeys=[A],
        match_matrix=mm,
        audit_matrix=am,
        source_audit_passed={A},
    )
    assert state.is_finalized is True


def test_margin_exactly_at_win_margin_is_promotion() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, WIN_MARGIN)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.entries and state.entries[0].status == "won"


def test_margin_just_below_win_margin_is_not_promotion() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, WIN_MARGIN - 0.001)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.entries == ()


def test_chain_builds_in_reveal_order() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, 0.2)  # B beat A
    mm.add(LEADER, C, 0.3)
    mm.add(B, C, 0.2)  # C beat B
    state = _derive(all_hotkeys=[A, B, C], match_matrix=mm)
    statuses = [(e.hotkey, e.status, e.defender) for e in state.entries]
    assert statuses == [
        (A, "won", LEADER),
        (B, "won", A),
        (C, "won", B),
    ]
    assert state.winner == C


def test_intermediate_loser_recorded_with_correct_margin() -> None:
    # Reveal order [A, B, C]: A beats leader, B challenges A and loses,
    # C challenges A and wins. B stays in entries as "lost" against A.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, -0.2)  # B lost
    mm.add(LEADER, C, 0.3)
    mm.add(A, C, 0.2)  # C beat A
    state = _derive(all_hotkeys=[A, B, C], match_matrix=mm)
    statuses = [(e.hotkey, e.status, e.defender) for e in state.entries]
    assert statuses == [
        (A, "won", LEADER),
        (B, "lost", A),
        (C, "won", A),
    ]
    b_entry = _entry(state, B)
    assert b_entry is not None
    assert b_entry.margin == pytest.approx(-0.2)


def test_pending_miner_when_match_vs_current_defender_not_cached() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    state = _derive(all_hotkeys=[A, B], match_matrix=mm)
    statuses = [(e.hotkey, e.status, e.defender) for e in state.entries]
    assert statuses == [
        (A, "won", LEADER),
        (B, "pending", A),
    ]


def test_next_match_is_first_pending_in_reveal_order() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(LEADER, C, 0.3)
    state = _derive(all_hotkeys=[A, B, C], match_matrix=mm)
    assert state.next_match == (A, B)


def test_next_match_none_when_no_pending() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm)
    assert state.next_match is None


def test_chain_property_returns_only_promoted_in_order() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, -0.2)  # B lost
    mm.add(LEADER, C, 0.3)
    mm.add(A, C, 0.2)  # C beat A
    state = _derive(all_hotkeys=[A, B, C], match_matrix=mm)
    assert [e.hotkey for e in state.leaders] == [A, C]


def test_generation_rejected_produces_rejected_entry_with_reason() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm, generation_rejected={A})
    entry = state.entries[0]
    assert entry.status == "rejected"
    assert entry.reason == "generation report"


def test_source_audit_failed_produces_rejected_entry_with_reason() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    state = _derive(all_hotkeys=[A], match_matrix=mm, source_audit_failed={A})
    entry = state.entries[0]
    assert entry.status == "rejected"
    assert entry.reason == "source audit"


def test_submitted_audit_margin_below_threshold_produces_rejection() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = MatchMatrix()
    am.add(submitted_audit_key(1), A, -0.5)  # well below -WIN_MARGIN
    state = _derive(all_hotkeys=[A], match_matrix=mm, audit_matrix=am)
    entry = state.entries[0]
    assert entry.status == "rejected"
    assert "submitted vs generated" in (entry.reason or "")
    assert "#1" in (entry.reason or "")


def test_defender_audit_failure_against_leader_is_conclusive() -> None:
    # latest_defender = "leader" is always treated as fully approved.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = MatchMatrix()
    am.add(defender_audit_key(LEADER, 1), A, -0.2)  # below WIN_MARGIN
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=A, latest_defender=LEADER))
    state = _derive(all_hotkeys=[A], match_matrix=mm, audit_matrix=am, audit_requests=ar)
    entry = state.entries[0]
    assert entry.status == "rejected"
    assert "leader vs generated" in (entry.reason or "")


def test_defender_audit_failure_against_unverified_defender_is_inconclusive() -> None:
    # B's latest_defender is A. A is not verified (no source_audit_passed, no good
    # audit margins for A). B's bad defender_audit vs A is inconclusive → B should
    # be promoted (cache says B beat A), not rejected.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, 0.2)
    am = MatchMatrix()
    am.add(defender_audit_key(A, 1), B, -0.2)
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=B, latest_defender=A))
    state = _derive(
        all_hotkeys=[A, B],
        match_matrix=mm,
        audit_matrix=am,
        audit_requests=ar,
    )
    b_entry = _entry(state, B)
    assert b_entry is not None
    assert b_entry.status == "won"


def test_never_qualified_rejected_miner_is_omitted() -> None:
    # gen_rejected AND never had a leader match cached → not shown in entries.
    state = _derive(all_hotkeys=[A], generation_rejected={A})
    assert state.entries == ()


def test_generation_rejected_takes_precedence_over_submitted_audit_reason() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = MatchMatrix()
    am.add(submitted_audit_key(1), A, -0.5)
    state = _derive(
        all_hotkeys=[A],
        match_matrix=mm,
        audit_matrix=am,
        generation_rejected={A},
    )
    assert state.entries[0].reason == "generation report"


def test_source_audit_failed_takes_precedence_over_submitted_audit_reason() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = MatchMatrix()
    am.add(submitted_audit_key(1), A, -0.5)
    state = _derive(
        all_hotkeys=[A],
        match_matrix=mm,
        audit_matrix=am,
        source_audit_failed={A},
    )
    assert state.entries[0].reason == "source audit"


def test_verification_requires_source_audit_passed() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = _passing_audit_matrix((A, LEADER))
    state = _derive(all_hotkeys=[A], match_matrix=mm, audit_matrix=am, source_audit_passed=set())
    assert state.entries[0].verified is False


def test_verification_requires_all_audit_repeats_to_pass() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    # Only repeat 1 has good margins; repeat 2 missing → not verified.
    am = MatchMatrix()
    am.add(submitted_audit_key(1), A, 0.5)
    am.add(defender_audit_key(LEADER, 1), A, 0.5)
    state = _derive(
        all_hotkeys=[A],
        match_matrix=mm,
        audit_matrix=am,
        audit_repeats=2,
        source_audit_passed={A},
    )
    assert state.entries[0].verified is False


def test_verification_cascades_to_subsequent_chain_members() -> None:
    # A unverified (no source audit). B beat A in cache. B should be promoted but
    # unverified because A isn't verified.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, 0.2)
    am = _passing_audit_matrix((B, A))
    state = _derive(
        all_hotkeys=[A, B],
        match_matrix=mm,
        audit_matrix=am,
        source_audit_passed={B},
    )
    a_entry = _entry(state, A)
    b_entry = _entry(state, B)
    assert a_entry is not None and b_entry is not None
    assert a_entry.verified is False
    assert b_entry.verified is False


def test_full_chain_verified_when_every_member_passes_audits() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, 0.2)
    am = _passing_audit_matrix((A, LEADER), (B, A))
    state = _derive(
        all_hotkeys=[A, B],
        match_matrix=mm,
        audit_matrix=am,
        source_audit_passed={A, B},
    )
    assert all(e.verified for e in state.leaders)
    assert state.is_finalized is True
    assert state.winner == B


def test_rejected_chain_member_does_not_advance_defender() -> None:
    # A rejected via defender_audit vs leader. B beat A in cache, but after re-walk
    # B should challenge leader (the prior defender) and be promoted vs leader.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, 0.2)
    am = MatchMatrix()
    am.add(defender_audit_key(LEADER, 1), A, -0.2)
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=A, latest_defender=LEADER))
    state = _derive(
        all_hotkeys=[A, B],
        match_matrix=mm,
        audit_matrix=am,
        audit_requests=ar,
    )
    a_entry = _entry(state, A)
    b_entry = _entry(state, B)
    assert a_entry is not None and a_entry.status == "rejected"
    assert b_entry is not None
    assert b_entry.status == "won"
    assert b_entry.defender == LEADER


def test_chain_member_pending_when_no_match_vs_new_defender_after_break() -> None:
    # Original chain A → B → C. A rejected. After re-walk: B becomes promoted
    # vs leader, C pending vs B (no cached B-vs-C match).
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(LEADER, C, 0.3)
    mm.add(A, B, 0.2)
    mm.add(A, C, 0.2)
    am = MatchMatrix()
    am.add(defender_audit_key(LEADER, 1), A, -0.2)
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=A, latest_defender=LEADER))
    state = _derive(
        all_hotkeys=[A, B, C],
        match_matrix=mm,
        audit_matrix=am,
        audit_requests=ar,
    )
    a_entry = _entry(state, A)
    b_entry = _entry(state, B)
    c_entry = _entry(state, C)
    assert a_entry is not None and a_entry.status == "rejected"
    assert b_entry is not None and b_entry.status == "won" and b_entry.defender == LEADER
    assert c_entry is not None
    assert c_entry.status == "pending"
    assert c_entry.defender == B


def test_loser_gets_second_chance_after_chain_break() -> None:
    # A promoted, B lost to A. A audit-rejected. B should now be promoted vs leader.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(A, B, -0.2)
    am = MatchMatrix()
    am.add(defender_audit_key(LEADER, 1), A, -0.2)
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=A, latest_defender=LEADER))
    state = _derive(
        all_hotkeys=[A, B],
        match_matrix=mm,
        audit_matrix=am,
        audit_requests=ar,
    )
    a_entry = _entry(state, A)
    b_entry = _entry(state, B)
    assert a_entry is not None and a_entry.status == "rejected"
    assert b_entry is not None
    assert b_entry.status == "won"
    assert b_entry.defender == LEADER


def test_all_qualifiers_rejected_finalizes_as_leader() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    state = _derive(
        all_hotkeys=[A, B],
        match_matrix=mm,
        generation_rejected={A, B},
    )
    assert all(e.status == "rejected" for e in state.entries)
    assert state.is_finalized is True
    assert state.winner == LEADER
    assert state.next_match is None


def test_full_chain_reconstructs_from_cached_matches_on_restart() -> None:
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    mm.add(LEADER, C, 0.3)
    mm.add(A, B, 0.2)
    mm.add(B, C, 0.2)
    am = _passing_audit_matrix((A, LEADER), (B, A), (C, B))
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=A, latest_defender=LEADER))
    ar.add(AuditRequest(hotkey=B, latest_defender=A))
    ar.add(AuditRequest(hotkey=C, latest_defender=B))
    state = _derive(
        all_hotkeys=[A, B, C],
        match_matrix=mm,
        audit_matrix=am,
        audit_requests=ar,
        source_audit_passed={A, B, C},
    )
    assert [e.hotkey for e in state.leaders] == [A, B, C]
    assert state.is_finalized is True
    assert state.winner == C


def test_pending_miner_visible_after_restart() -> None:
    # On restart with A promoted and B qualified-but-unmatched, B must show as
    # pending, not be omitted.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    mm.add(LEADER, B, 0.3)
    state = _derive(all_hotkeys=[A, B], match_matrix=mm)
    b_entry = _entry(state, B)
    assert b_entry is not None and b_entry.status == "pending"


def test_audit_rejected_ex_leader_visible_after_restart() -> None:
    # The original silent-rejection bug: a miner who became a local leader and
    # was then audit-rejected must show up as rejected, not vanish.
    mm = MatchMatrix()
    mm.add(LEADER, A, 0.3)
    am = MatchMatrix()
    am.add(defender_audit_key(LEADER, 1), A, -0.2)
    ar = AuditRequests()
    ar.add(AuditRequest(hotkey=A, latest_defender=LEADER))
    state = _derive(
        all_hotkeys=[A],
        match_matrix=mm,
        audit_matrix=am,
        audit_requests=ar,
    )
    a_entry = _entry(state, A)
    assert a_entry is not None and a_entry.status == "rejected"


def test_find_exploratory_duel_empty_state_returns_none() -> None:
    assert Timeline(entries=()).find_exploratory_duel(MatchMatrix()) is None


def test_find_exploratory_duel_single_entry_returns_none() -> None:
    # One entry → no pairs to form.
    state = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),))
    assert state.find_exploratory_duel(MatchMatrix()) is None


def test_find_exploratory_duel_all_pairs_cached_returns_none() -> None:
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="won", defender=A, margin=0.2),
        )
    )
    mm = MatchMatrix()
    mm.add(A, B, 0.2)
    assert state.find_exploratory_duel(mm) is None


def test_find_exploratory_duel_returns_first_missing_pair_in_reveal_order() -> None:
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="won", defender=A, margin=0.2),
            TimelineEntry(hotkey=C, status="won", defender=B, margin=0.2),
        )
    )
    mm = MatchMatrix()
    mm.add(A, B, 0.2)
    mm.add(B, C, 0.2)
    # A-C missing → returned first (iteration: left=A, right=B then C)
    assert state.find_exploratory_duel(mm) == (A, C)


def test_find_exploratory_duel_pair_left_is_earlier_reveal_order() -> None:
    # No matches cached. First missing pair is (A, B) — A is reveal-earlier.
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="pending", defender=A),
        )
    )
    assert state.find_exploratory_duel(MatchMatrix()) == (A, B)


def test_find_exploratory_duel_includes_losers() -> None:
    # B lost a timeline match to A but is still in the round — eligible for
    # exploratory pairings against other qualifiers.
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="lost", defender=A, margin=-0.2),
            TimelineEntry(hotkey=C, status="won", defender=A, margin=0.2),
        )
    )
    mm = MatchMatrix()
    mm.add(A, B, -0.2)
    mm.add(A, C, 0.2)
    # B-C is the only missing pair → returned.
    assert state.find_exploratory_duel(mm) == (B, C)


def test_find_exploratory_duel_includes_pending() -> None:
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="pending", defender=A),
        )
    )
    mm = MatchMatrix()  # no matches cached
    assert state.find_exploratory_duel(mm) == (A, B)


def test_find_exploratory_duel_skips_rejected_entries() -> None:
    # B is rejected — no pair involving B should be returned.
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="rejected", defender=A, reason="source audit"),
            TimelineEntry(hotkey=C, status="won", defender=A, margin=0.2),
        )
    )
    mm = MatchMatrix()
    mm.add(A, C, 0.2)
    # Only A and C are candidates. A-C is cached → no missing pair.
    assert state.find_exploratory_duel(mm) is None


def test_find_exploratory_duel_rejected_entry_pair_not_returned() -> None:
    # Even when the (non-rejected, rejected) pair isn't cached, it must NOT be returned.
    state = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="rejected", defender=A, reason="source audit"),
        )
    )
    assert state.find_exploratory_duel(MatchMatrix()) is None


def test_timelinestate_winner_empty_is_leader() -> None:
    assert Timeline(entries=()).winner == LEADER


def test_timelinestate_winner_with_chain_is_last_promoted() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3, verified=True),
            TimelineEntry(hotkey=B, status="won", defender=A, margin=0.2, verified=True),
        )
    )
    assert s.winner == B


def test_timelinestate_is_finalized_empty_state_is_true() -> None:
    # No pending, no unverified promoted → finalized vacuously.
    assert Timeline(entries=()).is_finalized is True


def test_timelinestate_is_finalized_false_with_unverified_promoted() -> None:
    s = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3, verified=False),))
    assert s.is_finalized is False


def test_timelinestate_is_finalized_false_with_pending() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3, verified=True),
            TimelineEntry(hotkey=B, status="pending", defender=A),
        )
    )
    assert s.is_finalized is False


def test_timelinestate_next_match_is_first_pending() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="pending", defender=A),
            TimelineEntry(hotkey=C, status="pending", defender=A),
        )
    )
    assert s.next_match == (A, B)


def test_timelinestate_next_match_none_when_no_pending() -> None:
    s = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3, verified=True),))
    assert s.next_match is None


def test_timelinestate_chain_excludes_non_promoted() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="lost", defender=A, margin=-0.2),
            TimelineEntry(hotkey=C, status="rejected", defender=A, reason="x"),
            TimelineEntry(hotkey=D, status="pending", defender=A),
        )
    )
    assert [e.hotkey for e in s.leaders] == [A]


def test_timelinestate_equality_is_value_based() -> None:
    s1 = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),))
    s2 = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),))
    assert s1 == s2


def test_timelinestate_inequality_when_verified_differs() -> None:
    s1 = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),))
    s2 = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3, verified=True),))
    assert s1 != s2


def test_render_empty_state_notes_no_qualifiers() -> None:
    rendered = render_timeline(Timeline(entries=()))
    assert "no qualifiers" in rendered.lower()


def test_render_promoted_entry_shows_margin_and_defender() -> None:
    s = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.12345, verified=True),))
    rendered = render_timeline(s)
    assert A[:10] in rendered
    assert "won" in rendered
    assert "+12.35%" in rendered or "+12.34%" in rendered  # rounding
    assert "verified" in rendered


def test_render_pending_entry_omits_defender() -> None:
    # We can't predict who a pending miner will face — only the first pending's
    # next match is determined. Omit the defender label to avoid misleading reads.
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="pending", defender=A),
        )
    )
    rendered = render_timeline(s)
    pending_line = next(line for line in rendered.splitlines() if "pending" in line)
    assert B[:10] in pending_line
    assert A[:10] not in pending_line


def test_render_lost_entry_marks_loss() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="lost", defender=A, margin=-0.2),
        )
    )
    rendered = render_timeline(s)
    assert "lost" in rendered.lower()
    assert B[:10] in rendered


def test_render_rejected_entry_includes_reason() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(
                hotkey=A,
                status="rejected",
                defender=LEADER,
                reason="source audit",
            ),
        )
    )
    rendered = render_timeline(s)
    assert "rejected" in rendered.lower()
    assert "source audit" in rendered


def test_render_marks_current_leader() -> None:
    s = Timeline(
        entries=(
            TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3),
            TimelineEntry(hotkey=B, status="won", defender=A, margin=0.2),
        )
    )
    rendered = render_timeline(s)
    assert "current leader" in rendered.lower()
    assert B[:10] in rendered


def test_render_marks_unverified_promotion() -> None:
    s = Timeline(entries=(TimelineEntry(hotkey=A, status="won", defender=LEADER, margin=0.3, verified=False),))
    rendered = render_timeline(s)
    assert "unverified" in rendered.lower()
