"""Scenario tests for RoundRunner: drive create() then run() against an in-memory repo,
faking only GitHub, the judge (run_match), Discord, and the clock.

A scenario supplies a margin table (the judge's verdict per (left, right) duel, consumed
in call order) and a list of deliveries (external inputs the orchestrator and source
auditor produce, one batch per idle tick). Everything else runs for real; assertions are
on the committed artifacts and the published transcript."""

import json
from collections.abc import Callable
from datetime import date
from unittest.mock import AsyncMock, patch

from pydantic import TypeAdapter
from subnet_common.competition.audit_matrix import get_audit_matrix
from subnet_common.competition.build_info import BuildInfo, BuildsInfoAdapter, BuildStatus
from subnet_common.competition.config import CompetitionConfig
from subnet_common.competition.generation_report import (
    GenerationReport,
    GenerationReportOutcome,
    GenerationReportsAdapter,
    RepeatStats,
)
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter
from subnet_common.competition.match_matrix import get_match_matrix
from subnet_common.competition.match_report import MatchReport
from subnet_common.competition.source_audit import AuditListAdapter, AuditResult, AuditVerdict
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from judge_service.discord import NullDiscordNotifier
from judge_service.round_runner import RoundRunner
from judge_service.settings import Settings
from judge_service.timeline import defender_audit_key, submitted_audit_key


LEADER = "leader"
HK_A = "0xaaaaaaaaaaaa"
HK_B = "0xbbbbbbbbbbbb"
HK_C = "0xcccccccccccc"
PROMPTS = ["p0.png", "p1.png"]
SEED = 42
WIN_MARGIN = 0.5

_SUBMISSIONS_ADAPTER = TypeAdapter(dict[str, MinerSubmission])

# A generations file body with every prompt stem delivered (has a usable js URL).
_GENERATIONS_JSON = GenerationsAdapter.dump_json(
    {p.removesuffix(".png"): GenerationResult(js="https://cdn/x.js") for p in PROMPTS}, indent=2
).decode()

# A generations file body where no prompt stem has a usable output (a failed delivery).
_EMPTY_GENERATIONS_JSON = GenerationsAdapter.dump_json(
    {p.removesuffix(".png"): GenerationResult(failure_reason="js_fetch_failed") for p in PROMPTS}, indent=2
).decode()


class _RecordingDiscord(NullDiscordNotifier):
    def __init__(self) -> None:
        self.timeline_changes: list[dict] = []
        self.audit_requested: list[dict] = []
        self.audit_completed: list[dict] = []
        self.round_finalized: list[dict] = []

    async def notify_timeline_change(self, *, round_num: int, body: str) -> None:
        self.timeline_changes.append({"round_num": round_num, "body": body})

    async def notify_audit_requested(self, *, round_num: int, hotkey: str, defeated: str, margin: float) -> None:
        self.audit_requested.append({"round_num": round_num, "hotkey": hotkey, "defeated": defeated, "margin": margin})

    async def notify_audit_completed(
        self,
        *,
        round_num: int,
        hotkey: str,
        defender: str,
        repeats: list[tuple[float, float]],
        verified: bool,
    ) -> None:
        self.audit_completed.append({"hotkey": hotkey, "defender": defender, "verified": verified})

    async def notify_round_finalized(self, *, round_num: int, winner: str, reason: str) -> None:
        self.round_finalized.append({"round_num": round_num, "winner": winner, "reason": reason})


def _config_json() -> str:
    return CompetitionConfig(
        name="test-comp",
        description="scenario",
        first_evaluation_date=date(2026, 1, 1),
        last_competition_date=date(2026, 12, 31),
        generation_stage_minutes=60,
        win_margin=WIN_MARGIN,
        weight_decay=0.1,
        weight_floor=0.1,
        prompts_per_round=len(PROMPTS),
        carryover_prompts=0,
        audit_repeats=1,
    ).model_dump_json(indent=2)


def _world(miners: list[str]) -> MockGitHubClient:
    """Round 1 at the start of judging: config, the leader's reference renders, and each
    miner's submitted outputs. Regenerations, reports and source audits arrive later via
    deliveries. Miners are revealed in list order."""
    submissions = {
        hk: MinerSubmission(
            repo=f"org/{hk}",
            commit=f"sha_{hk}",
            cdn_url=f"https://cdn/{hk}",
            revealed_at_block=100 + i,
            round="test-comp-1",
        )
        for i, hk in enumerate(miners)
    }
    builds = {
        hk: BuildInfo(
            repo=f"org/{hk}",
            commit=f"sha_{hk}",
            revealed_at_block=100 + i,
            tag=f"tag_{hk}",
            status=BuildStatus.SUCCESS,
            docker_image=f"docker.io/{hk}:latest",
        )
        for i, hk in enumerate(miners)
    }
    files = {
        "config.json": _config_json(),
        "rounds/1/seed.json": json.dumps({"seed": SEED}),
        "rounds/1/prompts.txt": "\n".join(PROMPTS),
        "rounds/1/submissions.json": _SUBMISSIONS_ADAPTER.dump_json(submissions, indent=2).decode(),
        "rounds/1/builds.json": BuildsInfoAdapter.dump_json(builds, indent=2).decode(),
        "leader.json": LeaderListAdapter.dump_json(
            [
                LeaderEntry(
                    hotkey=LEADER,
                    repo="org/leader",
                    commit="sha_leader",
                    docker=f"docker.io/{LEADER}:latest",
                    weight=1.0,
                    effective_block=0,
                )
            ],
            indent=2,
        ).decode(),
        f"rounds/1/{LEADER}/generated_1.json": _GENERATIONS_JSON,
    }
    for hk in miners:
        files[f"rounds/1/{hk}/submitted.json"] = _GENERATIONS_JSON
    return MockGitHubClient(files=files)


def _deliver_regeneration(git: MockGitHubClient, *hotkeys: str) -> None:
    """Audit pod: regenerate each hotkey's outputs and mark its report COMPLETED, keeping
    any reports already delivered."""
    content = git.files.get("rounds/1/generation_reports.json")
    reports = dict(GenerationReportsAdapter.validate_json(content)) if content else {}
    for hk in hotkeys:
        git.files[f"rounds/1/{hk}/generated_1.json"] = _GENERATIONS_JSON
        reports[hk] = GenerationReport(
            hotkey=hk,
            outcome=GenerationReportOutcome.COMPLETED,
            repeats=[RepeatStats(repeat_index=1, generated_prompts=len(PROMPTS))],
        )
    git.files["rounds/1/generation_reports.json"] = GenerationReportsAdapter.dump_json(reports, indent=2).decode()


def _deliver_source_audits(git: MockGitHubClient, verdicts: dict[str, AuditVerdict]) -> None:
    """Source auditor: record each hotkey's source verdict, keeping any already delivered."""
    content = git.files.get("rounds/1/source_audit.json")
    audits = {a.hotkey: a for a in AuditListAdapter.validate_json(content)} if content else {}
    for hk, verdict in verdicts.items():
        audits[hk] = AuditResult(hotkey=hk, verdict=verdict)
    git.files["rounds/1/source_audit.json"] = AuditListAdapter.dump_json(list(audits.values()), indent=2).decode()


async def _run_round(
    git: MockGitHubClient,
    margins: dict[tuple[str, str], list[float]],
    deliveries: list[Callable[[MockGitHubClient], None]],
    settings: Settings,
    stop_on: tuple[str, str] | None = None,
) -> tuple[_RecordingDiscord, list[tuple[str, str]], int]:
    git_batcher = GitBatcher(git=git, branch="main", base_sha=git.ref_sha)
    discord = _RecordingDiscord()
    runner = await RoundRunner.create(
        git_batcher=git_batcher,
        state=CompetitionState(current_round=1, stage=RoundStage.DUELS),
        openai=AsyncMock(),
        settings=settings,
        discord=discord,
    )
    shutdown = GracefulShutdown()
    calls: list[tuple[str, str]] = []
    waits = 0

    async def fake_wait(timeout: float | None = None) -> bool:
        # Each idle delivers the next batch of external inputs; once they run out a correct
        # run has finalized, so a further idle is a stall — shut down to fail loudly.
        nonlocal waits
        index = waits
        waits += 1
        if index < len(deliveries):
            deliveries[index](git)
            return False
        shutdown.request_shutdown()
        return True

    async def fake_run_match(*, left: str, right: str, **kwargs: object) -> MatchReport:
        calls.append((left, right))
        if stop_on == (left, right):
            # A shutdown signal arrives while the judge is scoring this match.
            shutdown.request_shutdown()
        return MatchReport(left=left, right=right, score=0, margin=margins[(left, right)].pop(0), duels=[])

    with (
        patch("judge_service.round_runner.run_match", side_effect=fake_run_match),
        patch.object(shutdown, "wait", side_effect=fake_wait),
    ):
        await runner.run(shutdown)

    return discord, calls, waits


def _verdicts(discord: _RecordingDiscord) -> list[tuple[str, str, bool]]:
    return [(c["hotkey"], c["defender"], c["verified"]) for c in discord.audit_completed]


def _requested(discord: _RecordingDiscord) -> list[tuple[str, str]]:
    return [(c["hotkey"], c["defeated"]) for c in discord.audit_requested]


async def _assert_leader_defends_without_judging(git: MockGitHubClient, settings: Settings) -> None:
    """The round finalizes for the leader with no qualifier and no judge call — every match
    short-circuited (walkover or scoreless draw)."""
    discord, calls, waits = await _run_round(git, {}, [], settings)
    assert calls == []
    assert waits == 0
    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == LEADER
    assert discord.round_finalized[0]["winner"] == LEADER
    assert discord.audit_requested == []


async def test_single_challenger_dethrones_leader_and_is_verified(settings: Settings) -> None:
    git = _world([HK_A])
    margins = {
        (LEADER, HK_A): [1.0, 1.0],  # qualification; A's defender audit vs leader
        ("submitted", HK_A): [0.0],  # A's submission matches its regeneration
    }
    deliveries = [
        lambda g: _deliver_regeneration(g, HK_A),
        lambda g: _deliver_source_audits(g, {HK_A: AuditVerdict.PASSED}),
    ]
    discord, _calls, waits = await _run_round(git, margins, deliveries, settings)

    assert waits == 2

    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_A
    assert winner["docker_image"] == f"docker.io/{HK_A}:latest"

    final_state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert final_state.stage == RoundStage.FINALIZING

    match_matrix = await get_match_matrix(git=git, round_num=1, ref=git.ref_sha)
    audit_matrix = await get_audit_matrix(git=git, round_num=1, ref=git.ref_sha)
    assert match_matrix.get(LEADER, HK_A) == 1.0
    assert audit_matrix.get(submitted_audit_key(1), HK_A) == 0.0
    assert audit_matrix.get(defender_audit_key(LEADER, 1), HK_A) == 1.0

    assert _requested(discord) == [(HK_A, LEADER)]
    assert _verdicts(discord) == [(HK_A, LEADER, True)]
    assert discord.round_finalized[0]["winner"] == HK_A
    assert len(discord.timeline_changes) == 2  # initial publish, then the flip to verified


async def test_no_submissions_finalizes_with_leader_defending(settings: Settings) -> None:
    git = _world([])
    discord, calls, waits = await _run_round(git, {}, [], settings)

    assert calls == []
    assert waits == 0

    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == LEADER
    assert winner["docker_image"] == f"docker.io/{LEADER}:latest"

    final_state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert final_state.stage == RoundStage.FINALIZING

    assert len(discord.round_finalized) == 1
    assert discord.round_finalized[0]["winner"] == LEADER
    assert discord.round_finalized[0]["reason"] == "No submissions found"
    assert discord.audit_requested == []
    assert discord.timeline_changes == []


async def test_two_qualify_second_loses_to_first_first_wins(settings: Settings) -> None:
    """A dethrones the leader; B qualifies but loses to defender A. A is the new leader."""
    git = _world([HK_A, HK_B])
    margins = {
        (LEADER, HK_A): [1.0, 1.0],  # qualification; A's defender audit vs leader
        (LEADER, HK_B): [1.0],  # qualification
        (HK_A, HK_B): [-1.0],  # timeline: B loses to defender A
        ("submitted", HK_A): [0.0],
    }
    deliveries = [
        lambda g: _deliver_regeneration(g, HK_A),
        lambda g: _deliver_source_audits(g, {HK_A: AuditVerdict.PASSED}),
    ]
    discord, _calls, waits = await _run_round(git, margins, deliveries, settings)

    assert waits == 2
    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_A
    assert _requested(discord) == [(HK_A, LEADER)]  # B lost, never audited
    assert _verdicts(discord) == [(HK_A, LEADER, True)]
    assert discord.round_finalized[0]["winner"] == HK_A


async def test_two_qualify_second_beats_first_second_wins(settings: Settings) -> None:
    """A dethrones the leader, B dethrones A; both pass audit. B is the new leader."""
    git = _world([HK_A, HK_B])
    margins = {
        (LEADER, HK_A): [1.0, 1.0],  # qualification; A's defender audit vs leader
        (LEADER, HK_B): [1.0],  # qualification
        (HK_A, HK_B): [1.0, 1.0],  # timeline: B beats A; B's defender audit vs A
        ("submitted", HK_A): [0.0],
        ("submitted", HK_B): [0.0],
    }
    deliveries = [
        lambda g: _deliver_regeneration(g, HK_A, HK_B),
        lambda g: _deliver_source_audits(g, {HK_A: AuditVerdict.PASSED, HK_B: AuditVerdict.PASSED}),
    ]
    discord, _calls, waits = await _run_round(git, margins, deliveries, settings)

    assert waits == 2
    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_B
    assert _requested(discord) == [(HK_A, LEADER), (HK_B, HK_A)]
    assert _verdicts(discord) == [(HK_A, LEADER, True), (HK_B, HK_A, True)]
    assert discord.round_finalized[0]["winner"] == HK_B


async def test_two_qualify_second_loses_but_first_rejected_second_wins(settings: Settings) -> None:
    """A dethrones the leader and B loses to A, but A's source audit fails; A is rejected,
    so B is re-evaluated against the leader, beats it, and is the new leader."""
    git = _world([HK_A, HK_B])
    margins = {
        (LEADER, HK_A): [1.0],  # qualification (A rejected via source audit, never audited)
        (LEADER, HK_B): [1.0, 1.0],  # qualification; B's defender audit vs the restored leader
        (HK_A, HK_B): [-1.0],  # timeline: B loses to defender A
        ("submitted", HK_B): [0.0],
    }
    deliveries = [
        lambda g: _deliver_source_audits(g, {HK_A: AuditVerdict.FAILED, HK_B: AuditVerdict.PASSED}),
        lambda g: _deliver_regeneration(g, HK_B),
    ]
    discord, _calls, waits = await _run_round(git, margins, deliveries, settings)

    assert waits == 2
    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_B
    assert _requested(discord) == [(HK_A, LEADER), (HK_B, LEADER)]
    assert _verdicts(discord) == [(HK_B, LEADER, True)]  # A rejected before its audit ran
    assert discord.round_finalized[0]["winner"] == HK_B


async def test_second_beats_first_then_first_rejected_second_reaudited_against_leader(settings: Settings) -> None:
    """A challenger's audit against a defender that is later rejected must not disqualify it.
    A dethrones the leader, B dethrones A; B is audited against A first and its defender duel
    lands below win_margin, but the rejection is deferred while A is unverified. A then fails
    its own audit — its submission beats what it reproduces — and is rejected; B's defender
    reverts to the leader, B is re-audited against it and passes. B is the new leader; the
    stale vs-A margin stays in the matrix as forensic data."""
    git = _world([HK_A, HK_B])
    margins = {
        (LEADER, HK_A): [1.0, 1.0],  # qualification; A's defender audit vs leader
        (LEADER, HK_B): [1.0, 1.0],  # qualification; B re-audited vs the restored leader
        (HK_A, HK_B): [1.0, 0.01],  # timeline: B beats A; B's forensic audit vs A (below win_margin)
        ("submitted", HK_A): [-1.0],  # A's submission beats its regeneration -> A rejected
        ("submitted", HK_B): [0.0],  # B's submission matches its regeneration
    }

    def deliver_b_audit_inputs(g: MockGitHubClient) -> None:
        _deliver_regeneration(g, HK_B)
        _deliver_source_audits(g, {HK_B: AuditVerdict.PASSED})

    deliveries = [deliver_b_audit_inputs, lambda g: _deliver_regeneration(g, HK_A)]
    discord, calls, waits = await _run_round(git, margins, deliveries, settings)

    assert waits == 2
    assert calls == [
        (LEADER, HK_A),  # qualification
        (LEADER, HK_B),
        (HK_A, HK_B),  # timeline: B dethrones A
        ("submitted", HK_B),  # B audited against A
        (HK_A, HK_B),
        ("submitted", HK_A),  # A audited, submission beats its regeneration
        (LEADER, HK_A),
        (LEADER, HK_B),  # B re-audited against the restored leader
    ]
    assert _requested(discord) == [(HK_A, LEADER), (HK_B, HK_A), (HK_B, LEADER)]
    assert _verdicts(discord) == [(HK_B, HK_A, False), (HK_A, LEADER, False), (HK_B, LEADER, True)]

    audit_matrix = await get_audit_matrix(git=git, round_num=1, ref=git.ref_sha)
    assert audit_matrix.get(defender_audit_key(HK_A, 1), HK_B) == 0.01  # stale, forensic only
    assert audit_matrix.get(defender_audit_key(LEADER, 1), HK_B) == 1.0

    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_B


async def test_two_losers_are_matched_by_an_exploratory_duel(settings: Settings) -> None:
    """A dethrones the leader; B and C both qualify but lose to defender A. With no chain
    work left and A's audit not yet startable, the unmatched loser pair B-C is filled by an
    exploratory duel. A is the new leader."""
    git = _world([HK_A, HK_B, HK_C])
    margins = {
        (LEADER, HK_A): [1.0, 1.0],  # qualification; A's defender audit vs leader
        (LEADER, HK_B): [1.0],  # qualification
        (LEADER, HK_C): [1.0],  # qualification
        (HK_A, HK_B): [-1.0],  # timeline: B loses to defender A
        (HK_A, HK_C): [-1.0],  # timeline: C loses to defender A
        (HK_B, HK_C): [0.0],  # exploratory duel between the two losers (no effect on the chain)
        ("submitted", HK_A): [0.0],  # A's submission matches its regeneration
    }
    deliveries = [
        lambda g: _deliver_regeneration(g, HK_A),
        lambda g: _deliver_source_audits(g, {HK_A: AuditVerdict.PASSED}),
    ]
    discord, calls, waits = await _run_round(git, margins, deliveries, settings)

    assert waits == 2
    assert calls == [
        (LEADER, HK_A),  # qualification
        (LEADER, HK_B),
        (LEADER, HK_C),
        (HK_A, HK_B),  # timeline: B loses to A
        (HK_A, HK_C),  # timeline: C loses to A
        (HK_B, HK_C),  # exploratory: the two losers, previously unmatched
        ("submitted", HK_A),  # A's audit
        (LEADER, HK_A),
    ]

    match_matrix = await get_match_matrix(git=git, round_num=1, ref=git.ref_sha)
    assert match_matrix.has(HK_B, HK_C)  # the exploratory result was persisted

    assert _requested(discord) == [(HK_A, LEADER)]  # B and C lost, never audited
    assert _verdicts(discord) == [(HK_A, LEADER, True)]
    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_A


async def test_challenger_with_no_usable_output_loses_by_walkover(settings: Settings) -> None:
    """A challenger whose submission has no usable output is beaten without a judge call:
    the qualification match is a walkover for the leader, the challenger fails to qualify,
    and the leader defends the round."""
    git = _world([HK_A])
    git.files[f"rounds/1/{HK_A}/submitted.json"] = _EMPTY_GENERATIONS_JSON
    await _assert_leader_defends_without_judging(git, settings)


async def test_qualification_with_both_sides_empty_is_a_draw_leader_defends(settings: Settings) -> None:
    """When neither the leader's renders nor the challenger's submission have usable output,
    the qualification match is a scoreless draw with no judge call, so the challenger fails
    to qualify and the leader defends."""
    git = _world([HK_A])
    git.files[f"rounds/1/{LEADER}/generated_1.json"] = _EMPTY_GENERATIONS_JSON
    git.files[f"rounds/1/{HK_A}/submitted.json"] = _EMPTY_GENERATIONS_JSON
    await _assert_leader_defends_without_judging(git, settings)


async def test_restart_reuses_cached_matches_and_audits(settings: Settings) -> None:
    """A restarted judge rebuilds the round from persisted git state alone. After a full run
    has populated the match matrix, audit matrix, audit requests, report and source audit, a
    fresh runner over the same git re-derives the verified winner without calling the judge
    again or re-publishing audit notifications."""
    git = _world([HK_A])
    margins = {
        (LEADER, HK_A): [1.0, 1.0],  # qualification; A's defender audit vs leader
        ("submitted", HK_A): [0.0],  # A's submission matches its regeneration
    }
    deliveries = [
        lambda g: _deliver_regeneration(g, HK_A),
        lambda g: _deliver_source_audits(g, {HK_A: AuditVerdict.PASSED}),
    ]
    await _run_round(git, margins, deliveries, settings)  # first boot, persists everything

    # Restart: a fresh runner over the same git, with no judge verdicts and no deliveries.
    discord, calls, waits = await _run_round(git, {}, [], settings)

    assert calls == []  # every match and audit margin was reused from cache
    assert waits == 0

    match_matrix = await get_match_matrix(git=git, round_num=1, ref=git.ref_sha)
    audit_matrix = await get_audit_matrix(git=git, round_num=1, ref=git.ref_sha)
    assert match_matrix.get(LEADER, HK_A) == 1.0
    assert audit_matrix.get(defender_audit_key(LEADER, 1), HK_A) == 1.0

    winner = json.loads(git.committed["rounds/1/winner.json"])
    assert winner["winner_hotkey"] == HK_A
    assert discord.audit_requested == []  # request already persisted before the restart
    assert discord.audit_completed == []  # no new margins, so no re-published verdict
    assert discord.round_finalized[0]["winner"] == HK_A


async def test_shutdown_while_scoring_a_match_discards_partial_and_does_not_finalize(settings: Settings) -> None:
    """A shutdown signal arriving while the judge is scoring a match stops the round cleanly:
    the in-flight result is discarded (never persisted), the remaining work is skipped, and
    the round does not finalize."""
    git = _world([HK_A, HK_B])
    margins = {(LEADER, HK_A): [1.0]}  # interrupted mid-score; B's qualification never starts
    discord, calls, waits = await _run_round(git, margins, [], settings, stop_on=(LEADER, HK_A))

    assert calls == [(LEADER, HK_A)]  # shutdown hit during A; B was never scored
    assert waits == 0

    assert "rounds/1/winner.json" not in git.committed  # the round did not finalize
    assert discord.round_finalized == []

    match_matrix = await get_match_matrix(git=git, round_num=1, ref=git.ref_sha)
    assert match_matrix.get(LEADER, HK_A) is None  # the interrupted result was discarded
