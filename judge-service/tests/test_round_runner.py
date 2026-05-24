"""Spec tests for RoundRunner.

Written against expected behavior, not the implementation. Each test isolates
one concern (a predicate, one method's branch, or one integration scenario).
External calls (run_match, audit producers, GitHub) are mocked.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from subnet_common.competition.audit_requests import AuditRequest, AuditRequests
from subnet_common.competition.build_info import BuildInfo, BuildsInfoAdapter, BuildStatus
from subnet_common.competition.generation_report import (
    GenerationReport,
    GenerationReportOutcome,
    GenerationReportsAdapter,
)
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter, GenerationSource
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter
from subnet_common.competition.match_matrix import MatchMatrix
from subnet_common.competition.match_report import DuelReport, DuelWinner, MatchReport
from subnet_common.competition.source_audit import AuditListAdapter, AuditResult, AuditVerdict
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from judge_service.discord import NULL_DISCORD_NOTIFIER, DiscordNotifier, NullDiscordNotifier
from judge_service.round_runner import RoundRunner
from judge_service.settings import Settings
from judge_service.timeline import Timeline, TimelineEntry, defender_audit_key, submitted_audit_key


LEADER = "leader"
HK_A = "0xaaaaaaaaaaaa"
HK_B = "0xbbbbbbbbbbbb"
HK_C = "0xcccccccccccc"


_DUEL_SCORE = {
    DuelWinner.RIGHT: 1,
    DuelWinner.LEFT: -1,
    DuelWinner.DRAW: 0,
    DuelWinner.SKIPPED: 0,
}


class _RecordingDiscord(NullDiscordNotifier):
    """Discord notifier that records every notify_* call for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.timeline_changes: list[dict] = []
        self.audit_requested: list[dict] = []
        self.round_finalized: list[dict] = []
        self.judge_errors: list[dict] = []

    async def notify_timeline_change(self, **kwargs: object) -> None:  # type: ignore[override]
        self.timeline_changes.append(dict(kwargs))

    async def notify_audit_requested(self, **kwargs: object) -> None:  # type: ignore[override]
        self.audit_requested.append(dict(kwargs))

    async def notify_round_finalized(self, **kwargs: object) -> None:  # type: ignore[override]
        self.round_finalized.append(dict(kwargs))

    async def notify_judge_error(self, **kwargs: object) -> None:  # type: ignore[override]
        self.judge_errors.append(dict(kwargs))


def _make_runner(
    settings: Settings,
    *,
    hotkeys: list[str] | None = None,
    prompts: list[str] | None = None,
    win_margin: float = 0.1,
    audit_repeats: int = 1,
    match_matrix: MatchMatrix | None = None,
    audit_matrix: MatchMatrix | None = None,
    audit_requests: AuditRequests | None = None,
    discord: DiscordNotifier | None = None,
) -> tuple[RoundRunner, MockGitHubClient, GitBatcher]:
    git = MockGitHubClient()
    git_batcher = GitBatcher(git=git, branch="main", base_sha="abc123")
    runner = RoundRunner(
        git_batcher=git_batcher,
        state=CompetitionState(current_round=1, stage=RoundStage.DUELS),
        openai=AsyncMock(),
        hotkeys=hotkeys or [],
        seed=42,
        prompts=prompts or ["p0.png"],
        win_margin=win_margin,
        audit_repeats=audit_repeats,
        match_matrix=match_matrix or MatchMatrix(),
        audit_matrix=audit_matrix or MatchMatrix(),
        audit_requests=audit_requests or AuditRequests(),
        settings=settings,
        discord=discord or NULL_DISCORD_NOTIFIER,
    )
    return runner, git, git_batcher


def _make_match_report(duel_winners: list[DuelWinner], left: str, right: str) -> MatchReport:
    """Build a synthetic MatchReport from a list of duel winners."""
    prompts = [f"p{i}.png" for i in range(len(duel_winners))]
    duels = [
        DuelReport(name=p.removesuffix(".png"), prompt=p, winner=w) for p, w in zip(prompts, duel_winners, strict=True)
    ]
    score = sum(_DUEL_SCORE[w] for w in duel_winners)
    margin = score / len(duel_winners) if duel_winners else 0.0
    return MatchReport(left=left, right=right, score=score, margin=margin, duels=duels)


def _add_generations(
    git: MockGitHubClient,
    round_num: int,
    hotkey: str,
    source: GenerationSource,
    prompts: list[str],
    repeat_index: int = 1,
    failed: bool = False,
) -> None:
    """Write a generation file. If failed=True, all entries have no `js`."""
    if failed:
        gens = {p.removesuffix(".png"): GenerationResult() for p in prompts}
    else:
        gens = {p.removesuffix(".png"): GenerationResult(js="https://cdn/x.js") for p in prompts}
    suffix = f"_{repeat_index}" if source == GenerationSource.GENERATED else ""
    path = f"rounds/{round_num}/{hotkey}/{source.value}{suffix}.json"
    git.files[path] = GenerationsAdapter.dump_json(gens, indent=2).decode()


def _inject_external_state(
    git: MockGitHubClient,
    round_num: int,
    *,
    reports: dict[str, GenerationReport] | None = None,
    source_audits: list[AuditResult] | None = None,
) -> None:
    """Write generation_reports.json and source_audit.json into MockGitHubClient.files."""
    if reports is not None:
        git.files[f"rounds/{round_num}/generation_reports.json"] = GenerationReportsAdapter.dump_json(
            reports, indent=2
        ).decode()
    if source_audits is not None:
        git.files[f"rounds/{round_num}/source_audit.json"] = AuditListAdapter.dump_json(
            source_audits, indent=2
        ).decode()


def _inject_leader_file(git: MockGitHubClient) -> None:
    leader = LeaderEntry(
        hotkey=LEADER,
        repo="org/leader",
        commit="abc",
        docker="docker.io/org/leader:latest",
        weight=1.0,
        effective_block=0,
    )
    git.files["leader.json"] = LeaderListAdapter.dump_json([leader], indent=2).decode()


def _inject_builds(git: MockGitHubClient, round_num: int, hotkeys: list[str]) -> None:
    builds = {
        hk: BuildInfo(
            repo=f"org/{hk[:6]}",
            commit=f"sha_{hk[:6]}",
            revealed_at_block=100,
            tag=f"tag_{hk[:6]}",
            status=BuildStatus.SUCCESS,
            docker_image=f"docker.io/org/{hk[:6]}:latest",
        )
        for hk in hotkeys
    }
    git.files[f"rounds/{round_num}/builds.json"] = BuildsInfoAdapter.dump_json(builds, indent=2).decode()


# ---------------------------------------------------------------------------
# _get_generation_source
# ---------------------------------------------------------------------------


def test_get_generation_source_leader_uses_generated(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    assert runner._get_generation_source("leader") == GenerationSource.GENERATED


def test_get_generation_source_miner_uses_submitted(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    assert runner._get_generation_source(HK_A) == GenerationSource.SUBMITTED


# ---------------------------------------------------------------------------
# _is_generation_rejected / _completed / _is_source_audit_failed
# ---------------------------------------------------------------------------


def test_is_generation_rejected_true_when_outcome_rejected(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.REJECTED)}
    assert runner._is_generation_rejected(HK_A) is True


def test_is_generation_rejected_false_when_outcome_completed(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    assert runner._is_generation_rejected(HK_A) is False


def test_is_generation_rejected_false_when_no_report(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    assert runner._is_generation_rejected(HK_A) is False


def test_is_generation_completed_true_when_outcome_completed(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    assert runner._is_generation_completed(HK_A) is True


def test_is_generation_completed_false_when_outcome_pending(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.PENDING)}
    assert runner._is_generation_completed(HK_A) is False


def test_is_generation_completed_false_when_no_report(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    assert runner._is_generation_completed(HK_A) is False


def test_is_source_audit_failed_true_when_verdict_failed(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    runner._source_audits = {HK_A: AuditResult(hotkey=HK_A, verdict=AuditVerdict.FAILED)}
    assert runner._is_source_audit_failed(HK_A) is True


def test_is_source_audit_failed_false_when_verdict_passed(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    runner._source_audits = {HK_A: AuditResult(hotkey=HK_A, verdict=AuditVerdict.PASSED)}
    assert runner._is_source_audit_failed(HK_A) is False


def test_is_source_audit_failed_false_when_no_audit(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    assert runner._is_source_audit_failed(HK_A) is False


# ---------------------------------------------------------------------------
# _announce_if_changed
# ---------------------------------------------------------------------------


async def test_announce_skips_when_state_equals_last_state(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, _, _ = _make_runner(settings, discord=discord)
    state = Timeline()
    runner._last_state = state
    await runner._announce_if_changed(state)
    assert discord.timeline_changes == []


async def test_announce_notifies_when_state_differs_from_last_state(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, _, _ = _make_runner(settings, discord=discord)
    state = Timeline(entries=(TimelineEntry(hotkey=HK_A, status="won", defender=LEADER, margin=0.3),))
    await runner._announce_if_changed(state)
    assert len(discord.timeline_changes) == 1
    assert discord.timeline_changes[0]["round_num"] == 1
    assert "body" in discord.timeline_changes[0]


async def test_announce_updates_last_state_after_notifying(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, _, _ = _make_runner(settings, discord=discord)
    state = Timeline(entries=(TimelineEntry(hotkey=HK_A, status="won", defender=LEADER, margin=0.3),))
    await runner._announce_if_changed(state)
    await runner._announce_if_changed(state)
    assert len(discord.timeline_changes) == 1


# ---------------------------------------------------------------------------
# _ensure_match
# ---------------------------------------------------------------------------


async def test_ensure_match_returns_cached_outcome_without_running(settings: Settings) -> None:
    mm = MatchMatrix()
    mm.add(LEADER, HK_A, 0.5)
    runner, _, _ = _make_runner(settings, match_matrix=mm)
    match = await runner._ensure_match(LEADER, HK_A, GracefulShutdown())
    assert match.from_cache is True
    assert match.margin == 0.5
    assert match.left == LEADER
    assert match.right == HK_A


async def test_ensure_match_runs_and_stores_when_uncached(settings: Settings) -> None:
    runner, git, batcher = _make_runner(settings, prompts=["p0.png"])
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    _add_generations(git, 1, HK_A, GenerationSource.SUBMITTED, ["p0.png"])

    async def fake_run_match(**kwargs: object) -> MatchReport:
        return _make_match_report([DuelWinner.RIGHT], LEADER, HK_A)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        match = await runner._ensure_match(LEADER, HK_A, GracefulShutdown())
    await batcher.flush()

    assert match.from_cache is False
    assert match.margin == 1.0
    assert runner._match_matrix.get(LEADER, HK_A) == 1.0
    assert "rounds/1/matches_matrix.csv" in git.committed


# ---------------------------------------------------------------------------
# _run_match_between (missing generations)
# ---------------------------------------------------------------------------


async def test_run_match_both_missing_generations_returns_zero_margin(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings, prompts=["p0.png"])
    match = await runner._run_match_between(LEADER, HK_A, GracefulShutdown())
    assert match.margin == 0.0


async def test_run_match_left_missing_generations_returns_plus_hundred(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, prompts=["p0.png"])
    _add_generations(git, 1, HK_A, GenerationSource.SUBMITTED, ["p0.png"])
    match = await runner._run_match_between(LEADER, HK_A, GracefulShutdown())
    assert match.margin == 100.0


async def test_run_match_right_missing_generations_returns_minus_hundred(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, prompts=["p0.png"])
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    match = await runner._run_match_between(LEADER, HK_A, GracefulShutdown())
    assert match.margin == -100.0


async def test_run_match_returns_zero_when_only_failed_entries(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, prompts=["p0.png"])
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"], failed=True)
    _add_generations(git, 1, HK_A, GenerationSource.SUBMITTED, ["p0.png"], failed=True)
    match = await runner._run_match_between(LEADER, HK_A, GracefulShutdown())
    assert match.margin == 0.0


# ---------------------------------------------------------------------------
# _request_audit
# ---------------------------------------------------------------------------


async def test_request_audit_new_persists_and_notifies(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, git, batcher = _make_runner(settings, discord=discord)
    await runner._request_audit(hotkey=HK_A, defender=LEADER, margin=0.3)
    await batcher.flush()
    assert runner._audit_requests.has(HK_A)
    assert len(discord.audit_requested) == 1
    assert discord.audit_requested[0]["hotkey"] == HK_A
    assert "rounds/1/require_audit.json" in git.committed


async def test_request_audit_duplicate_is_noop(settings: Settings) -> None:
    discord = _RecordingDiscord()
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    runner, _, _ = _make_runner(settings, discord=discord, audit_requests=requests)
    await runner._request_audit(hotkey=HK_A, defender=LEADER, margin=0.3)
    assert discord.audit_requested == []


async def test_request_audit_updates_when_defender_changes(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, _, batcher = _make_runner(settings, discord=discord)
    await runner._request_audit(hotkey=HK_A, defender=LEADER, margin=0.3)
    await runner._request_audit(hotkey=HK_A, defender=HK_B, margin=0.4)
    await batcher.flush()
    assert len(discord.audit_requested) == 2
    assert runner._audit_requests.get(HK_A).latest_defender == HK_B


# ---------------------------------------------------------------------------
# _find_pending_verification
# ---------------------------------------------------------------------------


def test_find_pending_verification_empty_returns_none(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    assert runner._find_pending_verification() is None


def test_find_pending_verification_skips_generation_rejected(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    runner, _, _ = _make_runner(settings, audit_requests=requests)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.REJECTED)}
    assert runner._find_pending_verification() is None


def test_find_pending_verification_skips_source_audit_failed(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    runner, _, _ = _make_runner(settings, audit_requests=requests)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    runner._source_audits = {HK_A: AuditResult(hotkey=HK_A, verdict=AuditVerdict.FAILED)}
    assert runner._find_pending_verification() is None


def test_find_pending_verification_skips_when_generation_not_completed(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    runner, _, _ = _make_runner(settings, audit_requests=requests)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.PENDING)}
    assert runner._find_pending_verification() is None


def test_find_pending_verification_returns_hotkey_with_missing_submitted_margin(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    am = MatchMatrix()
    am.add(defender_audit_key(LEADER, 1), HK_A, 0.5)  # defender present, submitted missing
    runner, _, _ = _make_runner(settings, audit_requests=requests, audit_matrix=am)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    assert runner._find_pending_verification() == HK_A


def test_find_pending_verification_returns_hotkey_with_missing_defender_margin(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    am = MatchMatrix()
    am.add(submitted_audit_key(1), HK_A, 0.5)  # submitted present, defender missing
    runner, _, _ = _make_runner(settings, audit_requests=requests, audit_matrix=am)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    assert runner._find_pending_verification() == HK_A


def test_find_pending_verification_returns_none_when_all_margins_present(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    am = MatchMatrix()
    am.add(submitted_audit_key(1), HK_A, 0.5)
    am.add(defender_audit_key(LEADER, 1), HK_A, 0.5)
    runner, _, _ = _make_runner(settings, audit_requests=requests, audit_matrix=am)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    assert runner._find_pending_verification() is None


def test_find_pending_verification_checks_all_repeats(settings: Settings) -> None:
    requests = AuditRequests()
    requests.add(AuditRequest(hotkey=HK_A, latest_defender=LEADER))
    am = MatchMatrix()
    am.add(submitted_audit_key(1), HK_A, 0.5)
    am.add(defender_audit_key(LEADER, 1), HK_A, 0.5)
    # r=2 missing
    runner, _, _ = _make_runner(settings, audit_requests=requests, audit_matrix=am, audit_repeats=2)
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)}
    assert runner._find_pending_verification() == HK_A


# ---------------------------------------------------------------------------
# _transition_to_next_stage
# ---------------------------------------------------------------------------


async def test_transition_to_finalizing_when_pause_disabled(settings: Settings) -> None:
    runner, git, batcher = _make_runner(settings)
    await runner._transition_to_next_stage(reason="done")
    assert runner._state.stage == RoundStage.FINALIZING
    assert "state.json" in git.committed


async def test_transition_to_paused_when_pause_enabled(settings: Settings) -> None:
    paused_settings = settings.model_copy(update={"pause_on_stage_end": True})
    runner, git, _ = _make_runner(paused_settings)
    await runner._transition_to_next_stage(reason="done")
    assert runner._state.stage == RoundStage.PAUSED
    state_data = json.loads(git.committed["state.json"])
    assert state_data["stage"] == "paused"


# ---------------------------------------------------------------------------
# _resolve_winner
# ---------------------------------------------------------------------------


async def test_resolve_winner_leader_pulls_from_leader_state(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings)
    _inject_leader_file(git)
    result = await runner._resolve_winner("leader")
    assert result.winner_hotkey == "leader"
    assert result.docker_image == "docker.io/org/leader:latest"


async def test_resolve_winner_miner_pulls_from_build(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings)
    _inject_builds(git, 1, [HK_A])
    result = await runner._resolve_winner(HK_A)
    assert result.winner_hotkey == HK_A
    assert result.docker_image == f"docker.io/org/{HK_A[:6]}:latest"


async def test_resolve_winner_miner_missing_build_raises(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings)
    _inject_builds(git, 1, [HK_B])  # build for B, not A
    with pytest.raises(RuntimeError, match="Build not found"):
        await runner._resolve_winner(HK_A)


async def test_resolve_winner_miner_missing_docker_image_raises(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings)
    builds = {
        HK_A: BuildInfo(
            repo="org/a",
            commit="sha_a",
            revealed_at_block=100,
            tag="tag_a",
            status=BuildStatus.FAILURE,
            docker_image=None,
        )
    }
    git.files["rounds/1/builds.json"] = BuildsInfoAdapter.dump_json(builds, indent=2).decode()
    with pytest.raises(RuntimeError, match="Build not found"):
        await runner._resolve_winner(HK_A)


# ---------------------------------------------------------------------------
# _finalize
# ---------------------------------------------------------------------------


async def test_finalize_writes_winner_and_transitions(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, git, batcher = _make_runner(settings, discord=discord)
    _inject_leader_file(git)
    await runner._finalize(winner="leader", reason="No submissions found")
    assert "rounds/1/winner.json" in git.committed
    winner_data = json.loads(git.committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"
    state_data = json.loads(git.committed["state.json"])
    assert state_data["stage"] == "finalizing"
    assert len(discord.round_finalized) == 1
    assert discord.round_finalized[0]["winner"] == "leader"


async def test_finalize_with_miner_winner_uses_build(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings)
    _inject_builds(git, 1, [HK_A])
    await runner._finalize(winner=HK_A)
    winner_data = json.loads(git.committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == HK_A
    assert winner_data["docker_image"] == f"docker.io/org/{HK_A[:6]}:latest"


# ---------------------------------------------------------------------------
# _run_qualification
# ---------------------------------------------------------------------------


async def test_qualification_runs_leader_match_for_each_hotkey(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, hotkeys=[HK_A, HK_B], prompts=["p0.png"])
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    _add_generations(git, 1, HK_A, GenerationSource.SUBMITTED, ["p0.png"])
    _add_generations(git, 1, HK_B, GenerationSource.SUBMITTED, ["p0.png"])

    calls: list[tuple[str, str]] = []

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        calls.append((left, right))
        return _make_match_report([DuelWinner.RIGHT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner._run_qualification(GracefulShutdown())

    assert sorted(calls) == [(LEADER, HK_A), (LEADER, HK_B)]


async def test_qualification_skips_generation_rejected_hotkeys(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, hotkeys=[HK_A, HK_B], prompts=["p0.png"])
    runner._reports = {HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.REJECTED)}
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    _add_generations(git, 1, HK_B, GenerationSource.SUBMITTED, ["p0.png"])

    calls: list[tuple[str, str]] = []

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        calls.append((left, right))
        return _make_match_report([DuelWinner.RIGHT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner._run_qualification(GracefulShutdown())

    assert calls == [(LEADER, HK_B)]


async def test_qualification_skips_source_audit_failed_hotkeys(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, hotkeys=[HK_A, HK_B], prompts=["p0.png"])
    runner._source_audits = {HK_A: AuditResult(hotkey=HK_A, verdict=AuditVerdict.FAILED)}
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    _add_generations(git, 1, HK_B, GenerationSource.SUBMITTED, ["p0.png"])

    calls: list[tuple[str, str]] = []

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        calls.append((left, right))
        return _make_match_report([DuelWinner.RIGHT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner._run_qualification(GracefulShutdown())

    assert calls == [(LEADER, HK_B)]


async def test_qualification_idempotent_on_cached_matches(settings: Settings) -> None:
    mm = MatchMatrix()
    mm.add(LEADER, HK_A, 0.5)
    mm.add(LEADER, HK_B, 0.5)
    runner, _, _ = _make_runner(settings, hotkeys=[HK_A, HK_B], match_matrix=mm)

    calls: list[tuple[str, str]] = []

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        calls.append((left, right))
        return _make_match_report([DuelWinner.RIGHT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner._run_qualification(GracefulShutdown())

    assert calls == []


async def test_qualification_stops_on_shutdown(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings, hotkeys=[HK_A, HK_B, HK_C], prompts=["p0.png"])
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    for hk in [HK_A, HK_B, HK_C]:
        _add_generations(git, 1, hk, GenerationSource.SUBMITTED, ["p0.png"])

    shutdown = GracefulShutdown()
    calls: list[tuple[str, str]] = []

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        calls.append((left, right))
        if len(calls) == 1:
            shutdown.request_shutdown()
        return _make_match_report([DuelWinner.RIGHT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner._run_qualification(shutdown)

    # 1st match runs; shutdown set inside; loop checks should_stop before 2nd iteration.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# run() — integration scenarios
# ---------------------------------------------------------------------------


async def test_run_with_no_submissions_finalizes_as_leader(settings: Settings) -> None:
    discord = _RecordingDiscord()
    runner, git, _ = _make_runner(settings, hotkeys=[], discord=discord)
    _inject_leader_file(git)
    await runner.run(GracefulShutdown())
    winner_data = json.loads(git.committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"
    state_data = json.loads(git.committed["state.json"])
    assert state_data["stage"] == "finalizing"
    assert len(discord.round_finalized) == 1


async def test_run_all_qualifications_fail_finalizes_as_leader(settings: Settings) -> None:
    runner, git, _ = _make_runner(
        settings,
        hotkeys=[HK_A, HK_B],
        prompts=["p0.png"],
        win_margin=0.5,
    )
    _inject_leader_file(git)
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    for hk in [HK_A, HK_B]:
        _add_generations(git, 1, hk, GenerationSource.SUBMITTED, ["p0.png"])

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        return _make_match_report([DuelWinner.LEFT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner.run(GracefulShutdown())

    winner_data = json.loads(git.committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"


async def test_run_all_generation_rejected_finalizes_as_leader(settings: Settings) -> None:
    runner, git, _ = _make_runner(
        settings,
        hotkeys=[HK_A, HK_B],
        prompts=["p0.png"],
        win_margin=0.5,
    )
    _inject_leader_file(git)
    _inject_external_state(
        git,
        1,
        reports={
            HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.REJECTED),
            HK_B: GenerationReport(hotkey=HK_B, outcome=GenerationReportOutcome.REJECTED),
        },
    )

    calls: list[tuple[str, str]] = []

    async def fake_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        calls.append((left, right))
        return _make_match_report([DuelWinner.LEFT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner.run(GracefulShutdown())

    assert calls == []  # no matches run; both rejected before qualification
    winner_data = json.loads(git.committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"


# async def test_run_successful_single_qualifier_dethrones_leader(settings: Settings) -> None:
#     runner, git, _ = _make_runner(
#         settings,
#         hotkeys=[HK_A],
#         prompts=["p0.png", "p1.png"],
#         win_margin=0.5,
#         audit_repeats=1,
#     )
#     _inject_builds(git, 1, [HK_A])
#     _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png", "p1.png"])
#     _add_generations(git, 1, HK_A, GenerationSource.SUBMITTED, ["p0.png", "p1.png"])
#     _add_generations(git, 1, HK_A, GenerationSource.GENERATED, ["p0.png", "p1.png"], repeat_index=1)
#
#     audited_external_state_calls: list[int] = []
#     original_reload = runner._reload_external_state
#
#     async def reload_then_inject() -> None:
#         await original_reload()
#         # After qualification's reload, inject COMPLETED + PASSED + audit margins so
#         # the next derive_state sees a verified single-qualifier (finalizing).
#         audited_external_state_calls.append(1)
#         if len(audited_external_state_calls) >= 2:
#             _inject_external_state(
#                 git,
#                 1,
#                 reports={HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED)},
#                 source_audits=[AuditResult(hotkey=HK_A, verdict=AuditVerdict.PASSED)],
#             )
#             runner._audit_matrix.add(submitted_audit_key(1), HK_A, 0.5)
#             runner._audit_matrix.add(defender_audit_key(LEADER, 1), HK_A, 0.5)
#
#     async def fake_run_match(**kwargs: object) -> MatchReport:
#         left, right = str(kwargs["left"]), str(kwargs["right"])
#         return _make_match_report([DuelWinner.RIGHT, DuelWinner.RIGHT], left, right)
#
#     with (
#         patch("judge_service.round_runner.run_match", side_effect=fake_run_match),
#         patch.object(runner, "_reload_external_state", side_effect=reload_then_inject),
#     ):
#         await runner.run(GracefulShutdown())
#
#     winner_data = json.loads(git.committed["rounds/1/winner.json"])
#     assert winner_data["winner_hotkey"] == HK_A


async def test_run_shutdown_mid_qualification_does_not_finalize(settings: Settings) -> None:
    runner, git, _ = _make_runner(
        settings,
        hotkeys=[HK_A, HK_B, HK_C],
        prompts=["p0.png"],
    )
    _add_generations(git, 1, LEADER, GenerationSource.GENERATED, ["p0.png"])
    for hk in [HK_A, HK_B, HK_C]:
        _add_generations(git, 1, hk, GenerationSource.SUBMITTED, ["p0.png"])

    shutdown = GracefulShutdown()
    call_count = 0

    async def fake_run_match(**kwargs: object) -> MatchReport:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            shutdown.request_shutdown()
        left, right = str(kwargs["left"]), str(kwargs["right"])
        return _make_match_report([DuelWinner.LEFT], left, right)

    with patch("judge_service.round_runner.run_match", side_effect=fake_run_match):
        await runner.run(shutdown)

    # No finalization artifacts written when shutdown fires mid-qualification.
    assert "rounds/1/winner.json" not in git.committed


# ---------------------------------------------------------------------------
# _reload_external_state
# ---------------------------------------------------------------------------


async def test_reload_external_state_loads_reports_and_source_audits(settings: Settings) -> None:
    runner, git, _ = _make_runner(settings)
    _inject_external_state(
        git,
        1,
        reports={
            HK_A: GenerationReport(hotkey=HK_A, outcome=GenerationReportOutcome.COMPLETED),
            HK_B: GenerationReport(hotkey=HK_B, outcome=GenerationReportOutcome.REJECTED, reason="bad"),
        },
        source_audits=[
            AuditResult(hotkey=HK_A, verdict=AuditVerdict.PASSED),
            AuditResult(hotkey=HK_B, verdict=AuditVerdict.FAILED, reason="copy"),
        ],
    )
    await runner._reload_external_state()
    assert runner._is_generation_completed(HK_A) is True
    assert runner._is_generation_rejected(HK_B) is True
    assert runner._is_source_audit_failed(HK_B) is True
    assert HK_A in runner._source_audits


async def test_reload_external_state_handles_missing_files(settings: Settings) -> None:
    runner, _, _ = _make_runner(settings)
    await runner._reload_external_state()
    assert runner._reports == {}
    assert runner._source_audits == {}
