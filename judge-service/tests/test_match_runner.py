import json
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import TypeAdapter
from subnet_common.competition.audit_requests import AuditRequests
from subnet_common.competition.build_info import BuildInfo, BuildsInfoAdapter, BuildStatus
from subnet_common.competition.generation_audit import GenerationAudit, GenerationAuditOutcome, GenerationAuditsAdapter
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.leader import LeaderEntry, LeaderListAdapter
from subnet_common.competition.match_matrix import MatchMatrix
from subnet_common.competition.match_report import DuelReport, DuelWinner, MatchReport
from subnet_common.competition.source_audit import AuditListAdapter, AuditResult, AuditVerdict
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.git_batcher import GitBatcher
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from judge_service.match_execution import WINNER_TO_SCORE
from judge_service.match_runner import MatchRunner, Timeline
from judge_service.settings import Settings


def _make_runner(
    settings: Settings,
    hotkeys: list[str] | None = None,
    seed: int = 42,
    win_margin: float = 0.05,
) -> tuple[MatchRunner, MockGitHubClient]:
    mock_github = MockGitHubClient()
    git_batcher = GitBatcher(git=mock_github, branch="main", base_sha="abc123")
    runner = MatchRunner(
        git_batcher=git_batcher,
        state=CompetitionState(current_round=1, stage=RoundStage.DUELS),
        openai=AsyncMock(),
        hotkeys=hotkeys or [],
        seed=seed,
        prompts=[],
        win_margin=win_margin,
        match_matrix=MatchMatrix(),
        audit_requests=AuditRequests(),
        settings=settings,
    )
    return runner, mock_github


def _make_report(
    duel_winners: list[DuelWinner],
    left: str = "left",
    right: str = "right",
) -> MatchReport:
    prompts = [f"p{i}.png" for i in range(len(duel_winners))]
    duels = [
        DuelReport(name=p.removesuffix(".png"), prompt=p, winner=w) for p, w in zip(prompts, duel_winners, strict=True)
    ]
    score = sum(WINNER_TO_SCORE[w] for w in duel_winners)
    margin = score / len(duel_winners) if duel_winners else 0
    return MatchReport(left=left, right=right, score=score, margin=margin, duels=duels)


def _inject_leader(git: MockGitHubClient) -> None:
    """Add a leader.json so _resolve_winner("leader") can find it."""
    leader = LeaderEntry(
        hotkey="leader",
        repo="org/leader",
        commit="abc",
        docker="img:latest",
        weight=1.0,
        effective_block=0,
    )
    git.files["leader.json"] = LeaderListAdapter.dump_json([leader], indent=2).decode()


def _parse_matrix(committed: dict[str, str]) -> dict[tuple[str, str], float]:
    """Parse the committed matches_matrix.csv into a {(left, right): margin} dict."""
    matrix = MatchMatrix.from_csv(committed["rounds/1/matches_matrix.csv"])
    return dict(matrix._margins)


def test_timeline_lifecycle() -> None:
    """Walk through a full timeline: reset, matches, rejection, verification."""
    tl = Timeline()
    tl.reset(["miner_a", "miner_b", "miner_c"])

    # Fresh timeline: not finished, not rejected, no winner, defaults to "leader"
    assert tl.finished is False
    assert tl.is_rejected(rejected_hotkeys=set()) is False
    assert tl.has_verified_winner(approved_hotkeys=set()) is False
    assert tl.winner == "leader"

    # Two miners win their matches and become local leaders, one still pending
    tl.pending_miners.popleft()
    tl.local_leaders.append("miner_a")
    tl.pending_miners.popleft()
    tl.local_leaders.append("miner_b")

    # Still has pending miners — not finished, can't verify even if all approved
    assert tl.finished is False
    assert tl.has_verified_winner(approved_hotkeys={"miner_a", "miner_b"}) is False

    # Rejection checks against local leaders
    assert tl.is_rejected(rejected_hotkeys={"miner_a"}) is True  # first leader rejected
    assert tl.is_rejected(rejected_hotkeys={"miner_b"}) is True  # second leader rejected
    assert tl.is_rejected(rejected_hotkeys={"miner_c"}) is False  # pending miner, not a leader

    # Drain remaining pending
    tl.pending_miners.popleft()

    # Now finished — winner is the last local leader
    assert tl.finished is True
    assert tl.winner == "miner_b"

    # Verified winner requires ALL local leaders approved
    assert tl.has_verified_winner(approved_hotkeys={"miner_a"}) is False  # miner_b missing
    assert tl.has_verified_winner(approved_hotkeys={"miner_b"}) is False  # miner_a missing
    assert tl.has_verified_winner(approved_hotkeys={"miner_a", "miner_b"}) is True

    # Rejection still works when finished
    assert tl.is_rejected(rejected_hotkeys={"miner_a"}) is True
    assert tl.is_rejected(rejected_hotkeys={"miner_b"}) is True


def test_extract_decisive_prompts(settings: Settings) -> None:
    """Three audit regimes: big margin (none), few right-wins (all), many right-wins (sample)."""
    runner, _ = _make_runner(settings, seed=42)
    # full_tolerance = max_mismatched_margin (0.05) + win_margin (0.05) = 0.10
    # 20 duels → count = max(1, ceil(20 * 0.05)) = 1
    R, L, D = DuelWinner.RIGHT, DuelWinner.LEFT, DuelWinner.DRAW

    # Big margin (0.40) — standard audit is sufficient, no pivotal prompts
    big_margin = _make_report([R] * 14 + [L] * 6)
    assert runner._extract_decisive_prompts(big_margin) == []

    # Tight margin (0.05), single right-win — all right-winning prompts are pivotal
    few_wins = _make_report([R] + [D] * 19)
    assert runner._extract_decisive_prompts(few_wins) == ["p0"]

    # Tight margin (0.05), many right-wins — sample a subset (count=1 from 8)
    many_wins = _make_report([R] * 8 + [L] * 7 + [D] * 5)
    assert runner._extract_decisive_prompts(many_wins) == ["p1"]


_SUBMISSIONS_ADAPTER = TypeAdapter(dict[str, MinerSubmission])


def _populate_round(
    git: MockGitHubClient, round_num: int, num_prompts: int, hotkeys: list[str], win_margin: float
) -> None:
    """Populate all git files needed for MatchRunner.create() and run()."""
    prompts = [f"p{i}.png" for i in range(num_prompts)]

    git.files["config.json"] = json.dumps(
        {
            "name": "test",
            "description": "test",
            "first_evaluation_date": "2025-01-01",
            "last_competition_date": "2025-12-31",
            "generation_stage_minutes": 60,
            "win_margin": win_margin,
            "weight_decay": 0.01,
            "weight_floor": 0.05,
            "prompts_per_round": num_prompts,
            "carryover_prompts": 0,
        }
    )

    git.files[f"rounds/{round_num}/seed.json"] = json.dumps({"seed": 42})
    git.files[f"rounds/{round_num}/prompts.txt"] = "\n".join(prompts)

    submissions = {
        hk: MinerSubmission(repo=f"org/{hk}", commit=f"sha_{hk}", cdn_url="x", revealed_at_block=i, round="test-1")
        for i, hk in enumerate(hotkeys)
    }
    git.files[f"rounds/{round_num}/submissions.json"] = _SUBMISSIONS_ADAPTER.dump_json(submissions, indent=2).decode()

    # generations — leader + all miners
    _add_generations(git, round_num, "leader", "generated", prompts)
    for hk in hotkeys:
        _add_generations(git, round_num, hk, "submitted", prompts)

    # builds.json — SUCCESS builds for finalization
    _add_builds(git, round_num, hotkeys)


def _add_generations(git: MockGitHubClient, round_num: int, hotkey: str, source: str, prompts: list[str]) -> None:
    """Populate a non-empty generation file so _run_match_between doesn't short-circuit."""
    gens = {p.removesuffix(".png"): GenerationResult(glb="x", png="x") for p in prompts}
    git.files[f"rounds/{round_num}/{hotkey}/{source}.json"] = GenerationsAdapter.dump_json(gens, indent=2).decode()


def _add_builds(git: MockGitHubClient, round_num: int, hotkeys: list[str]) -> None:
    """Populate builds.json with SUCCESS builds for all hotkeys."""
    builds = {
        hk: BuildInfo(
            repo=f"org/{hk}",
            commit=f"sha_{hk}",
            revealed_at_block=100,
            tag=f"tag_{hk}",
            status=BuildStatus.SUCCESS,
            docker_image=f"docker.io/org/{hk}:latest",
        )
        for hk in hotkeys
    }
    git.files[f"rounds/{round_num}/builds.json"] = BuildsInfoAdapter.dump_json(builds, indent=2).decode()


def _inject_audits(
    git: MockGitHubClient,
    round_num: int,
    passed: list[str],
    rejected: list[str] | None = None,
) -> None:
    """Inject generation_audits and source_audit."""
    gen_audits = {hk: GenerationAudit(hotkey=hk, outcome=GenerationAuditOutcome.PASSED) for hk in passed}
    for hk in rejected or []:
        gen_audits[hk] = GenerationAudit(hotkey=hk, outcome=GenerationAuditOutcome.REJECTED)
    git.files[f"rounds/{round_num}/generation_audits.json"] = GenerationAuditsAdapter.dump_json(
        gen_audits, indent=2
    ).decode()

    source_audits = [AuditResult(hotkey=hk, verdict=AuditVerdict.PASSED) for hk in passed]
    git.files[f"rounds/{round_num}/source_audit.json"] = AuditListAdapter.dump_json(source_audits, indent=2).decode()


class _Scenario:
    """Shared setup for full MatchRunner integration tests."""

    NUM_PROMPTS = 10
    NUM_HOTKEYS = 10

    def __init__(
        self,
        runner: MatchRunner,
        git: MockGitHubClient,
        shutdown: GracefulShutdown,
        hotkeys: list[str],
    ) -> None:
        self.runner = runner
        self.git = git  # use git.committed after run() to inspect written files
        self.shutdown = shutdown
        self.duel_outcomes: dict[tuple[str, str], DuelWinner] = {}  # (left, right) → duel outcome
        self.run_match_calls: list[tuple[str, str]] = []  # recorded (left, right) pairs

        # Qualification: the first 5 hotkeys qualify (RIGHT wins), the rest don't (LEFT wins)
        for hk in hotkeys[:5]:
            self.duel_outcomes[("leader", hk)] = DuelWinner.RIGHT
        for hk in hotkeys[5:]:
            self.duel_outcomes[("leader", hk)] = DuelWinner.LEFT

    @classmethod
    async def create(cls, settings: Settings) -> "_Scenario":
        hotkeys = [f"hk_{i:02d}" for i in range(cls.NUM_HOTKEYS)]
        git = MockGitHubClient()
        git_batcher = GitBatcher(git=git, branch="main", base_sha="abc123")
        _populate_round(git, round_num=1, num_prompts=cls.NUM_PROMPTS, hotkeys=hotkeys, win_margin=1.0)

        state = CompetitionState(current_round=1, stage=RoundStage.DUELS)
        runner = await MatchRunner.create(
            git_batcher=git_batcher,
            state=state,
            openai=AsyncMock(),
            settings=settings,
        )
        shutdown = GracefulShutdown()
        return cls(runner, git, shutdown, hotkeys)

    async def run(
        self,
        mock_wait: Callable[..., Coroutine[Any, Any, bool]],
    ) -> dict[str, str]:
        """Patch run_match + shutdown.wait, execute runner.run(), return committed files."""
        duel_outcomes = self.duel_outcomes
        run_match_calls = self.run_match_calls

        async def mock_run_match(**kwargs: object) -> MatchReport:
            left, right = str(kwargs["left"]), str(kwargs["right"])
            run_match_calls.append((left, right))
            return _make_report([duel_outcomes[(left, right)]] * self.NUM_PROMPTS, left, right)

        with (
            patch("judge_service.match_runner.run_match", side_effect=mock_run_match),
            patch.object(self.shutdown, "wait", side_effect=mock_wait),
        ):
            await self.runner.run(self.shutdown)

        return self.git.committed


async def test_no_submissions_finalizes_as_leader(settings: Settings) -> None:
    """No hotkeys submitted → immediately finalizes with leader as winner."""
    runner, git = _make_runner(settings, hotkeys=[])
    _inject_leader(git)

    await runner.run(GracefulShutdown())

    committed = git.committed
    winner_data = json.loads(committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"
    assert winner_data["docker_image"] == "img:latest"

    state_data = json.loads(committed["state.json"])
    assert state_data["stage"] == "finalizing"


async def test_full_success_scenario(settings: Settings) -> None:
    """Full MatchRunner success path via run(): qual → timeline → exploratory → verify → finalize."""
    s = await _Scenario.create(settings)

    R, L = DuelWinner.RIGHT, DuelWinner.LEFT

    # Timeline: hk_00 beats leader (cached), hk_02 beats hk_00, hk_04 beats hk_02
    s.duel_outcomes[("hk_00", "hk_01")] = L  # hk_01 loses
    s.duel_outcomes[("hk_00", "hk_02")] = R  # hk_02 wins → local leader
    s.duel_outcomes[("hk_02", "hk_03")] = L  # hk_03 loses
    s.duel_outcomes[("hk_02", "hk_04")] = R  # hk_04 wins → local leader + timeline winner

    # Exploratory: fill remaining qualified pairs
    for pair in [
        ("hk_00", "hk_03"),
        ("hk_00", "hk_04"),
        ("hk_01", "hk_02"),
        ("hk_01", "hk_03"),
        ("hk_01", "hk_04"),
        ("hk_03", "hk_04"),
    ]:
        s.duel_outcomes[pair] = R

    original_wait = s.shutdown.wait

    async def mock_wait(**kwargs: object) -> bool:
        _inject_audits(s.git, 1, ["hk_00", "hk_02", "hk_04"])
        return await original_wait(timeout=0)

    committed = await s.run(mock_wait)

    # 10 qualification + 4 timeline (1 cached) + 6 exploratory = 20 unique matches
    assert len(s.run_match_calls) == 20

    winner_data = json.loads(committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "hk_04"
    assert winner_data["docker_image"] == "docker.io/org/hk_04:latest"

    state_data = json.loads(committed["state.json"])
    assert state_data["stage"] == "finalizing"
    assert state_data["current_round"] == 1

    matrix = _parse_matrix(committed)
    assert len(matrix) == 20

    # Qualification: first 5 RIGHT (1.0), rest LEFT (-1.0)
    for i in range(5):
        assert matrix[("leader", f"hk_{i:02d}")] == 1.0
    for i in range(5, 10):
        assert matrix[("leader", f"hk_{i:02d}")] == -1.0

    # Timeline + exploratory: RIGHT wins (1.0) and LEFT wins (-1.0)
    for pair in [
        ("hk_00", "hk_02"),
        ("hk_02", "hk_04"),
        ("hk_00", "hk_03"),
        ("hk_00", "hk_04"),
        ("hk_01", "hk_02"),
        ("hk_01", "hk_03"),
        ("hk_01", "hk_04"),
        ("hk_03", "hk_04"),
    ]:
        assert matrix[pair] == 1.0
    for pair in [("hk_00", "hk_01"), ("hk_02", "hk_03")]:
        assert matrix[pair] == -1.0

    audit_data = json.loads(committed["rounds/1/require_audit.json"])
    assert {e["hotkey"] for e in audit_data} == {"hk_00", "hk_02", "hk_04"}
    for entry in audit_data:
        assert len(entry["critical_prompts"]) == s.NUM_PROMPTS


async def test_timeline_rejection_resets_and_picks_new_winner(settings: Settings) -> None:
    """Timeline winner (hk_03) includes a rejected local leader (hk_02).

    Timeline 1 produces hk_03 as the winner, but hk_02 (a local leader on the path)
    is rejected by audit. The timeline resets and replays from cached results,
    skipping hk_02. hk_03 still wins — no new matches are needed.

    Uses win_margin=1.0 and max_mismatched_margin=1.0 so that all prompts
    are flagged as decisive in audit requests.
    """
    test_settings = settings.model_copy(update={"max_mismatched_margin": 1.0})
    s = await _Scenario.create(test_settings)

    R, L = DuelWinner.RIGHT, DuelWinner.LEFT

    # Timeline 1: hk_03 beats hk_02, hk_04 loses to hk_03
    s.duel_outcomes[("hk_00", "hk_01")] = L  # hk_01 loses
    s.duel_outcomes[("hk_00", "hk_02")] = R  # hk_02 wins → local leader (to be rejected)
    s.duel_outcomes[("hk_02", "hk_03")] = L  # hk_03 loses → local leader in an alternative timeline
    s.duel_outcomes[("hk_02", "hk_04")] = R  # hk_04 wins → timeline winner (but loses to hk_03)

    # Exploratory pairs (between qualified hk_00..hk_04, excluding timeline pairs)
    for pair in [
        ("hk_00", "hk_03"),
        ("hk_00", "hk_04"),
        ("hk_01", "hk_02"),
        ("hk_01", "hk_03"),
        ("hk_01", "hk_04"),
    ]:
        s.duel_outcomes[pair] = R

    s.duel_outcomes[("hk_03", "hk_04")] = L

    original_wait = s.shutdown.wait

    async def mock_wait(**kwargs: object) -> bool:
        _inject_audits(s.git, 1, ["hk_00", "hk_03"], rejected=["hk_02"])
        return await original_wait(timeout=0)

    committed = await s.run(mock_wait)

    # Same 20 run_match calls — timeline 2 is fully cached, no new matches
    assert len(s.run_match_calls) == 20

    winner_data = json.loads(committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "hk_03"
    assert winner_data["docker_image"] == "docker.io/org/hk_03:latest"

    state_data = json.loads(committed["state.json"])
    assert state_data["stage"] == "finalizing"

    matrix = _parse_matrix(committed)
    assert len(matrix) == 20

    # Qualification: first 5 RIGHT (1.0), rest LEFT (-1.0)
    for i in range(5):
        assert matrix[("leader", f"hk_{i:02d}")] == 1.0
    for i in range(5, 10):
        assert matrix[("leader", f"hk_{i:02d}")] == -1.0

    # Timeline + exploratory
    for pair in [
        ("hk_00", "hk_02"),
        ("hk_02", "hk_04"),
        ("hk_00", "hk_03"),
        ("hk_00", "hk_04"),
        ("hk_01", "hk_02"),
        ("hk_01", "hk_03"),
        ("hk_01", "hk_04"),
    ]:
        assert matrix[pair] == 1.0
    for pair in [("hk_00", "hk_01"), ("hk_02", "hk_03"), ("hk_03", "hk_04")]:
        assert matrix[pair] == -1.0

    audit_data = json.loads(committed["rounds/1/require_audit.json"])
    assert {e["hotkey"] for e in audit_data} == {"hk_00", "hk_02", "hk_03", "hk_04"}
    for entry in audit_data:
        assert len(entry["critical_prompts"]) == s.NUM_PROMPTS


async def test_all_rejected_before_duels_finalizes_as_leader(settings: Settings) -> None:
    """All submissions rejected by audit before duels → finalizes with leader as winner."""
    s = await _Scenario.create(settings)
    _inject_leader(s.git)

    hotkeys = [f"hk_{i:02d}" for i in range(s.NUM_HOTKEYS)]
    _inject_audits(s.git, 1, passed=[], rejected=hotkeys)

    original_wait = s.shutdown.wait

    async def mock_wait(**kwargs: object) -> bool:
        return await original_wait(timeout=0)

    committed = await s.run(mock_wait)

    assert len(s.run_match_calls) == 0  # no duels at all

    winner_data = json.loads(committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"

    state_data = json.loads(committed["state.json"])
    assert state_data["stage"] == "finalizing"


async def test_all_fail_qualification_finalizes_as_leader(settings: Settings) -> None:
    """All miners lose to the leader during qualification → finalizes with leader as winner."""
    s = await _Scenario.create(settings)

    _inject_leader(s.git)

    # Override: every hotkey loses qualification
    for hk in [f"hk_{i:02d}" for i in range(s.NUM_HOTKEYS)]:
        s.duel_outcomes[("leader", hk)] = DuelWinner.LEFT

    original_wait = s.shutdown.wait

    async def mock_wait(**kwargs: object) -> bool:
        return await original_wait(timeout=0)

    committed = await s.run(mock_wait)

    assert len(s.run_match_calls) == s.NUM_HOTKEYS  # only qualification matches

    matrix = _parse_matrix(committed)
    assert len(matrix) == s.NUM_HOTKEYS
    for i in range(s.NUM_HOTKEYS):
        assert matrix[("leader", f"hk_{i:02d}")] == -1.0

    winner_data = json.loads(committed["rounds/1/winner.json"])
    assert winner_data["winner_hotkey"] == "leader"

    state_data = json.loads(committed["state.json"])
    assert state_data["stage"] == "finalizing"


@pytest.mark.parametrize(
    "left_has_gens, right_has_gens, expected_margin",
    [
        (False, False, 0.0),
        (False, True, 100.0),
        (True, False, -100.0),
    ],
    ids=["both_missing", "left_missing", "right_missing"],
)
async def test_run_match_no_generations(
    settings: Settings,
    left_has_gens: bool,
    right_has_gens: bool,
    expected_margin: float,
) -> None:
    """Missing generations produce fixed margins without calling the judge."""
    runner, git = _make_runner(settings, hotkeys=["hk_a", "hk_b"])

    prompts = ["p0.png"]
    if left_has_gens:
        _add_generations(git, round_num=1, hotkey="hk_a", source="submitted", prompts=prompts)
    if right_has_gens:
        _add_generations(git, round_num=1, hotkey="hk_b", source="submitted", prompts=prompts)

    match = await runner._run_match_between(left="hk_a", right="hk_b", shutdown=GracefulShutdown())

    assert match.left == "hk_a"
    assert match.right == "hk_b"
    assert match.margin == expected_margin


async def test_shutdown_mid_match_stops_and_discards_interrupted_work(settings: Settings) -> None:
    """Shutdown during run_match discards the interrupted match and stops the runner.

    Setup: 5 hotkeys, all RIGHT-winning. Shutdown fires on the 3rd run_match call.
    Expected: first 2 matches saved normally, 3rd match report and matrix entry discarded,
    no finalization artifacts written, runner exits promptly.
    """
    hotkeys = [f"hk_{i:02d}" for i in range(5)]
    git = MockGitHubClient()
    git_batcher = GitBatcher(git=git, branch="main", base_sha="abc123")
    num_prompts = 3
    _populate_round(git, round_num=1, num_prompts=num_prompts, hotkeys=hotkeys, win_margin=0.5)

    state = CompetitionState(current_round=1, stage=RoundStage.DUELS)
    runner = await MatchRunner.create(
        git_batcher=git_batcher,
        state=state,
        openai=AsyncMock(),
        settings=settings,
    )

    shutdown = GracefulShutdown()
    run_match_calls: list[tuple[str, str]] = []

    async def mock_run_match(**kwargs: object) -> MatchReport:
        left, right = str(kwargs["left"]), str(kwargs["right"])
        run_match_calls.append((left, right))
        if len(run_match_calls) == 3:
            shutdown.request_shutdown()
        return _make_report([DuelWinner.RIGHT] * num_prompts, left, right)

    with patch("judge_service.match_runner.run_match", side_effect=mock_run_match):
        await runner.run(shutdown)

    # Runner stopped after exactly 3 run_match calls
    assert len(run_match_calls) == 3

    # run() flushes on shutdown, so all completed work is committed
    committed = git.committed

    # The interrupted (3rd) match has no report committed
    interrupted_right = run_match_calls[2][1]
    interrupted_report_path = f"rounds/1/{interrupted_right}/duels_leader.json"
    assert interrupted_report_path not in committed

    # The first 2 completed matches ARE committed (not lost)
    for left, right in run_match_calls[:2]:
        report_path = f"rounds/1/{right}/duels_{left[:10]}.json"
        assert report_path in committed

    # Matrix has only the 2 completed matches (3rd was not added)
    matrix = MatchMatrix.from_csv(committed["rounds/1/matches_matrix.csv"])
    assert len(matrix) == 2

    # No finalization artifacts — runner exited without finalizing
    assert "rounds/1/winner.json" not in committed
    assert "state.json" not in committed

    # Nothing left pending — flush drained everything
    assert git_batcher.pending_count == 0
