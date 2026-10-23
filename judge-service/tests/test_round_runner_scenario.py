"""Scenario tests for RoundRunner: drive create() then run() against an in-memory repo,
faking only GitHub, the judge (run_match), Discord, and the clock.

A scenario supplies a margin table (the judge's verdict per (left, right) duel, consumed
in call order) and a list of deliveries (external inputs the validator and source
auditor produce, one batch per idle tick). Everything else runs for real; assertions are
on committed artifacts, requested work and the timeline announcements."""

import json
from collections.abc import Callable
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import TypeAdapter, ValidationError
from subnet_common.competition.config import CompetitionConfig
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter
from subnet_common.competition.match_matrix import get_match_matrix
from subnet_common.competition.match_report import MatchReport
from subnet_common.competition.source_audit import (
    AuditListAdapter,
    AuditResult,
    AuditVerdict,
)
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.competition.validation import (
    ValidationResult,
    ValidationResultsAdapter,
    ValidationVerdict,
    get_validation_results,
)
from subnet_common.competition.validation_requests import get_validation_requests
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from judge_service.discord import NullDiscordNotifier
from judge_service.round_runner import RoundRunner
from judge_service.settings import Settings


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
    {p.removesuffix(".png"): GenerationResult(js="https://cdn/x.js") for p in PROMPTS},
    indent=2,
).decode()

# A generations file body where no prompt stem has a usable output (a failed delivery).
_EMPTY_GENERATIONS_JSON = GenerationsAdapter.dump_json(
    {p.removesuffix(".png"): GenerationResult(failure_reason="js_fetch_failed") for p in PROMPTS},
    indent=2,
).decode()


class _RecordingDiscord(NullDiscordNotifier):
    def __init__(self) -> None:
        self.timeline_changes: list[dict] = []
        self.validation_requested: list[dict] = []
        self.round_finalized: list[dict] = []

    async def notify_timeline_change(self, *, round_num: int, body: str) -> None:
        self.timeline_changes.append({"round_num": round_num, "body": body})

    async def notify_validation_requested(self, *, round_num: int, hotkey: str, defeated: str, margin: float) -> None:
        self.validation_requested.append(
            {
                "round_num": round_num,
                "hotkey": hotkey,
                "defeated": defeated,
                "margin": margin,
            }
        )

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
        prompts_per_round=len(PROMPTS),
        carryover_prompts=0,
    ).model_dump_json(indent=2)


def _world(miners: list[str]) -> MockGitHubClient:
    """Canonical result bundles are ready; validation and source verdicts arrive later.
    Miners are revealed in list order.
    """
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
    files = {
        "config.json": _config_json(),
        "rounds/1/seed.json": json.dumps({"seed": SEED}),
        "rounds/1/prompts.txt": "\n".join(PROMPTS),
        "rounds/1/submissions.json": _SUBMISSIONS_ADAPTER.dump_json(submissions, indent=2).decode(),
        "leader.json": LeaderListAdapter.dump_json(
            [
                LeaderEntry(
                    hotkey=LEADER,
                    repo="org/leader",
                    commit="sha_leader",
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


def _deliver_validation(git: MockGitHubClient, verdicts: dict[str, str]) -> None:
    """Validator: publish one terminal result per hotkey, retaining previous results."""
    content = git.files.get("rounds/1/validation_results.json")
    results = ValidationResultsAdapter.validate_json(content) if content is not None else {}
    for hk, verdict in verdicts.items():
        results[hk] = ValidationResult(
            verdict=ValidationVerdict(verdict),
            reason="recorded output mismatch" if verdict == "failed" else "",
        )
    git.files["rounds/1/validation_results.json"] = ValidationResultsAdapter.dump_json(results, indent=2).decode()


def _deliver_source_audits(git: MockGitHubClient, verdicts: dict[str, str]) -> None:
    """Source auditor: record each hotkey's source verdict, keeping any already delivered."""
    content = git.files.get("rounds/1/source_audit.json")
    audits = {a.hotkey: a for a in AuditListAdapter.validate_json(content)} if content else {}
    for hk, verdict in verdicts.items():
        audits[hk] = AuditResult(hotkey=hk, verdict=AuditVerdict(verdict))
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
        return MatchReport(
            left=left,
            right=right,
            score=0,
            margin=margins[(left, right)].pop(0),
            duels=[],
        )

    with (
        patch("judge_service.round_runner.run_match", side_effect=fake_run_match),
        patch.object(shutdown, "wait", side_effect=fake_wait),
    ):
        await runner.run(shutdown)

    return discord, calls, waits


def _requested(discord: _RecordingDiscord) -> list[tuple[str, str]]:
    return [(c["hotkey"], c["defeated"]) for c in discord.validation_requested]


def _winner(git: MockGitHubClient) -> str:
    return json.loads(git.committed["rounds/1/winner.json"])["winner_hotkey"]


def _deliver_passes(git: MockGitHubClient, *hotkeys: str) -> None:
    verdicts = dict.fromkeys(hotkeys, "passed")
    _deliver_validation(git, verdicts)
    _deliver_source_audits(git, verdicts)


def _assert_verdicts_are_read_only(git: MockGitHubClient) -> None:
    assert "rounds/1/validation_results.json" not in git.committed
    assert "rounds/1/source_audit.json" not in git.committed


async def test_single_challenger_waits_for_both_verdicts(settings: Settings) -> None:
    git = _world([HK_A])
    passes = {HK_A: "passed"}
    _deliver_validation(git, passes)

    discord, calls, waits = await _run_round(
        git,
        {(LEADER, HK_A): [1.0]},
        [lambda g: _deliver_source_audits(g, passes)],
        settings,
    )

    assert waits == 1
    assert calls == [(LEADER, HK_A)]
    assert json.loads(git.committed["rounds/1/winner.json"]) == {
        "winner_hotkey": HK_A,
        "repo": f"org/{HK_A}",
        "commit": f"sha_{HK_A}",
    }
    assert CompetitionState.model_validate_json(git.committed["state.json"]).stage == RoundStage.FINALIZING
    assert _requested(discord) == [(HK_A, LEADER)]
    assert len(discord.timeline_changes) == 2
    assert "[verified]" in discord.timeline_changes[-1]["body"]
    assert discord.round_finalized[0]["winner"] == HK_A
    assert set(await get_validation_results(git, 1, git.ref_sha)) == {HK_A}
    _assert_verdicts_are_read_only(git)


async def test_no_submissions_finalizes_with_leader_defending(
    settings: Settings,
) -> None:
    git = _world([])
    discord, calls, waits = await _run_round(git, {}, [], settings)
    assert calls == []
    assert waits == 0
    assert _winner(git) == LEADER
    assert discord.round_finalized[0]["reason"] == "No submissions found"
    assert discord.validation_requested == []
    assert discord.timeline_changes == []


async def test_two_qualify_second_beats_first_second_wins(settings: Settings) -> None:
    git = _world([HK_A, HK_B])
    discord, calls, waits = await _run_round(
        git,
        {(LEADER, HK_A): [1.0], (LEADER, HK_B): [1.0], (HK_A, HK_B): [1.0]},
        [lambda g: _deliver_passes(g, HK_A, HK_B)],
        settings,
    )
    assert _winner(git) == HK_B
    assert calls == [(LEADER, HK_A), (LEADER, HK_B), (HK_A, HK_B)]
    assert waits == 1
    assert _requested(discord) == [(HK_A, LEADER), (HK_B, HK_A)]


async def test_collector_submission_order_determines_winner(settings: Settings) -> None:
    git = _world([HK_A, HK_B])
    submissions = json.loads(git.files["rounds/1/submissions.json"])
    git.files["rounds/1/submissions.json"] = json.dumps({HK_B: submissions[HK_B], HK_A: submissions[HK_A]})

    await _run_round(
        git,
        {(LEADER, HK_A): [1.0], (LEADER, HK_B): [1.0], (HK_A, HK_B): [0.0], (HK_B, HK_A): [0.0]},
        [lambda g: _deliver_passes(g, HK_A, HK_B)],
        settings,
    )

    assert _winner(git) == HK_B


async def test_loser_becomes_candidate_when_defender_fails(settings: Settings) -> None:
    git = _world([HK_A, HK_B])
    discord, calls, waits = await _run_round(
        git,
        {(LEADER, HK_A): [1.0], (LEADER, HK_B): [1.0], (HK_A, HK_B): [-1.0]},
        [
            lambda g: _deliver_source_audits(g, {HK_A: "failed", HK_B: "passed"}),
            lambda g: _deliver_validation(g, {HK_B: "passed"}),
        ],
        settings,
    )
    assert _winner(git) == HK_B
    assert calls == [(LEADER, HK_A), (LEADER, HK_B), (HK_A, HK_B)]
    assert waits == 2
    assert _requested(discord) == [(HK_A, LEADER), (HK_B, LEADER)]
    assert "rejected: source audit" in discord.timeline_changes[-1]["body"]
    _assert_verdicts_are_read_only(git)


async def test_defender_change_reuses_candidate_validation(settings: Settings) -> None:
    git = _world([HK_A, HK_B])

    def reject_a(g: MockGitHubClient) -> None:
        # B's own checks passed, but A is unverified: the chain cannot finalize.
        assert "rounds/1/winner.json" not in g.committed
        _deliver_validation(g, {HK_A: "failed"})

    discord, calls, waits = await _run_round(
        git,
        {(LEADER, HK_A): [1.0], (LEADER, HK_B): [1.0], (HK_A, HK_B): [1.0]},
        [lambda g: _deliver_passes(g, HK_B), reject_a],
        settings,
    )
    assert _winner(git) == HK_B
    assert calls == [(LEADER, HK_A), (LEADER, HK_B), (HK_A, HK_B)]
    assert waits == 2
    # The defender changes from A to leader. B is neither re-requested nor re-judged.
    assert _requested(discord) == [(HK_A, LEADER), (HK_B, HK_A)]
    requests = await get_validation_requests(git, 1, git.ref_sha)
    assert len(requests) == 2
    assert f"vs {LEADER}  [verified]" in discord.timeline_changes[-1]["body"]
    _assert_verdicts_are_read_only(git)


async def test_exploratory_duels_fill_cache_while_validation_is_pending(
    settings: Settings,
) -> None:
    git = _world([HK_A, HK_B, HK_C])
    discord, calls, waits = await _run_round(
        git,
        {
            (LEADER, HK_A): [1.0],
            (LEADER, HK_B): [1.0],
            (LEADER, HK_C): [1.0],
            (HK_A, HK_B): [-1.0],
            (HK_A, HK_C): [-1.0],
            (HK_B, HK_C): [0.0],
        },
        [lambda g: _deliver_passes(g, HK_A)],
        settings,
    )
    assert _winner(git) == HK_A
    assert waits == 1
    assert calls == [
        (LEADER, HK_A),
        (LEADER, HK_B),
        (LEADER, HK_C),
        (HK_A, HK_B),
        (HK_A, HK_C),
        (HK_B, HK_C),
    ]
    matrix = await get_match_matrix(git, 1, git.ref_sha)
    assert matrix.get(HK_B, HK_C) == 0.0
    assert _requested(discord) == [(HK_A, LEADER)]


@pytest.mark.parametrize("leader_empty", [False, True])
async def test_unusable_challenger_never_beats_leader(settings: Settings, leader_empty: bool) -> None:
    git = _world([HK_A])
    git.files[f"rounds/1/{HK_A}/submitted.json"] = _EMPTY_GENERATIONS_JSON
    if leader_empty:
        git.files[f"rounds/1/{LEADER}/generated_1.json"] = _EMPTY_GENERATIONS_JSON
    discord, calls, waits = await _run_round(git, {}, [], settings)
    assert _winner(git) == LEADER
    assert calls == []
    assert waits == 0
    assert discord.validation_requested == []


async def test_restart_reuses_cached_matches_requests_and_terminal_results(
    settings: Settings,
) -> None:
    git = _world([HK_A])
    await _run_round(git, {(LEADER, HK_A): [1.0]}, [lambda g: _deliver_passes(g, HK_A)], settings)
    discord, calls, waits = await _run_round(git, {}, [], settings)
    assert calls == []
    assert waits == 0
    assert _winner(git) == HK_A
    assert discord.validation_requested == []
    _assert_verdicts_are_read_only(git)


async def test_invalid_validation_result_is_a_service_error(settings: Settings) -> None:
    git = _world([HK_A])
    git.files["rounds/1/validation_results.json"] = '{"miner": {"verdict": "failed", "reason": ""}}'
    with pytest.raises(ValidationError):
        await _run_round(git, {}, [], settings)
    assert "rounds/1/winner.json" not in git.committed
    _assert_verdicts_are_read_only(git)


async def test_shutdown_while_scoring_discards_partial_result(
    settings: Settings,
) -> None:
    git = _world([HK_A, HK_B])
    discord, calls, waits = await _run_round(git, {(LEADER, HK_A): [1.0]}, [], settings, stop_on=(LEADER, HK_A))
    assert calls == [(LEADER, HK_A)]
    assert waits == 0
    assert "rounds/1/winner.json" not in git.committed
    assert discord.round_finalized == []
    matrix = await get_match_matrix(git, 1, git.ref_sha)
    assert matrix.get(LEADER, HK_A) is None
