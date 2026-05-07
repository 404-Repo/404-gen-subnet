from unittest.mock import AsyncMock, MagicMock, patch

from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import MatchReport
from subnet_common.competition.verification_audit import VerificationOutcome
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.audit_execution import (
    SUBMITTED_LEFT,
    compute_verification,
    produce_d1_duel,
    produce_d2_duel,
)


def _gen(views: str | None = "https://cdn/preview") -> GenerationResult:
    return GenerationResult(js="https://cdn/model.js", views=views)


# Producers — D1 (audit_submitted.json) and D2 (audit_{defender[:10]}.json) save through
# `save_match_report(kind='audit')`. These tests verify that wiring; the per-duel score
# math itself is covered by `test_match_execution.py` (both producers route through
# `run_match`).


async def test_produce_d1_duel_saves_audit_submitted_with_correct_sides() -> None:
    """D1 saves at `rounds/{n}/{hotkey}/audit_submitted.json` with submitted=LEFT,
    generated=RIGHT. Positive margin (RIGHT win) is the legit signal."""
    write_calls: list[dict] = []

    async def _write(*, path: str, content: str, message: str) -> None:
        write_calls.append({"path": path, "content": content, "message": message})

    git_batcher = MagicMock()
    git_batcher.write = AsyncMock(side_effect=_write)

    captured: dict = {}

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        captured.update(kwargs)
        return MatchReport(
            left=str(kwargs["left"]),
            right=str(kwargs["right"]),
            score=2,
            margin=0.5,
            duels=[],
        )

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await produce_d1_duel(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=3,
            hotkey="audited_full_xxxxxxxx",
            prompts=["a.png"],
            seed=42,
            submitted_gens={"a": _gen()},
            generated_gens={"a": _gen()},
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=GracefulShutdown(),
        )

    assert captured["left"] == SUBMITTED_LEFT
    assert captured["right"] == "audited_full_xxxxxxxx"
    assert len(write_calls) == 1
    assert write_calls[0]["path"] == "rounds/3/audited_full_xxxxxxxx/audit_submitted.json"
    assert "Audit report" in write_calls[0]["message"]


async def test_produce_d2_duel_saves_audit_defender_with_correct_sides() -> None:
    """D2 saves at `rounds/{n}/{audited}/audit_{defender[:10]}.json` with defender=LEFT,
    audited.generated=RIGHT. Mirrors verification audit's direction (RIGHT = legit)."""
    write_calls: list[dict] = []

    async def _write(*, path: str, content: str, message: str) -> None:
        write_calls.append({"path": path, "content": content, "message": message})

    git_batcher = MagicMock()
    git_batcher.write = AsyncMock(side_effect=_write)

    captured: dict = {}

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        captured.update(kwargs)
        return MatchReport(
            left=str(kwargs["left"]),
            right=str(kwargs["right"]),
            score=2,
            margin=0.5,
            duels=[],
        )

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await produce_d2_duel(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=7,
            audited_hotkey="audited_full_hotkey_xxxxx",
            defender_hotkey="defender_full_hotkey_yyy",
            prompts=["a.png"],
            seed=42,
            defender_gens={"a": _gen()},
            audited_generated={"a": _gen()},
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=GracefulShutdown(),
        )

    assert captured["left"] == "defender_full_hotkey_yyy"
    assert captured["right"] == "audited_full_hotkey_xxxxx"
    assert len(write_calls) == 1
    assert write_calls[0]["path"] == "rounds/7/audited_full_hotkey_xxxxx/audit_defender_f.json"
    assert "Audit report" in write_calls[0]["message"]


async def test_produce_d2_duel_skips_save_on_shutdown() -> None:
    """Shutdown set during run_match → returns the report without persisting. Best-effort
    semantics: a re-run on next iteration will retry production."""
    git_batcher = MagicMock()
    git_batcher.write = AsyncMock()
    shutdown = GracefulShutdown()

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        shutdown.request_shutdown()
        return MatchReport(left=str(kwargs["left"]), right=str(kwargs["right"]), score=0, margin=0.0, duels=[])

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await produce_d2_duel(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=1,
            audited_hotkey="audited",
            defender_hotkey="leader",
            prompts=["a.png"],
            seed=42,
            defender_gens={"a": _gen()},
            audited_generated={"a": _gen()},
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=shutdown,
        )

    git_batcher.write.assert_not_called()


# Verdict — `compute_verification` derives PENDING/PASSED/FAILED from D1 + D2-vs-current-defender
# files on disk. Verdict is a pure function of state, never persisted.


def _build_git_batcher_with_reports(reports_by_path: dict[str, MatchReport | None]) -> MagicMock:
    """Mock a GitBatcher whose `read(path)` returns serialized JSON of preconfigured
    MatchReports (or None for paths not in the dict — simulates a missing file)."""

    async def _read(path: str) -> str | None:
        report = reports_by_path.get(path)
        return report.model_dump_json() if report is not None else None

    git_batcher = MagicMock()
    git_batcher.read = _read
    return git_batcher


def _audit_report(left: str, right: str, margin: float) -> MatchReport:
    """Build a minimal MatchReport with the desired margin."""
    return MatchReport(left=left, right=right, score=int(margin * 100), margin=margin, duels=[])


async def test_compute_verification_pending_when_d1_missing() -> None:
    """D1 not yet produced → PENDING regardless of D2."""
    git_batcher = _build_git_batcher_with_reports(
        {
            "rounds/1/X/audit_leader.json": _audit_report("leader", "X", 0.50),
        }
    )
    outcome = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="leader", win_margin=0.05
    )
    assert outcome == VerificationOutcome.PENDING


async def test_compute_verification_pending_when_d2_missing_for_current_defender() -> None:
    """D2 against the current defender hasn't been produced yet → PENDING. This is the
    natural state right after `latest_defender` updates (e.g., after a timeline reset
    re-climbed the miner): old D2 files for previous defenders are on disk but aren't
    consulted; the new D2 hasn't been written yet."""
    git_batcher = _build_git_batcher_with_reports(
        {
            "rounds/1/X/audit_submitted.json": _audit_report("submitted", "X", 0.10),
            # OLD D2 against a previous defender Y exists on disk — should NOT be consulted.
            "rounds/1/X/audit_Y.json": _audit_report("Y", "X", 0.50),
        }
    )
    outcome = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="leader", win_margin=0.05
    )
    assert outcome == VerificationOutcome.PENDING


async def test_compute_verification_passed_when_both_gates_clear() -> None:
    """D1.margin ≥ −W and D2.margin ≥ +W → PASSED."""
    win_margin = 0.05
    git_batcher = _build_git_batcher_with_reports(
        {
            "rounds/1/X/audit_submitted.json": _audit_report("submitted", "X", -0.03),  # within −W
            "rounds/1/X/audit_leader.json": _audit_report("leader", "X", 0.10),  # ≥ +W
        }
    )
    outcome = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="leader", win_margin=win_margin
    )
    assert outcome == VerificationOutcome.PASSED


async def test_compute_verification_failed_on_d1_too_negative() -> None:
    """D1.margin < −W → FAILED (generated lost too much to submitted; cherry-picking signal)."""
    win_margin = 0.05
    git_batcher = _build_git_batcher_with_reports(
        {
            "rounds/1/X/audit_submitted.json": _audit_report("submitted", "X", -0.20),  # below −W
            "rounds/1/X/audit_leader.json": _audit_report("leader", "X", 0.10),  # would pass D2
        }
    )
    outcome = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="leader", win_margin=win_margin
    )
    assert outcome == VerificationOutcome.FAILED


async def test_compute_verification_failed_on_d2_below_plus_w() -> None:
    """D2.margin < +W → FAILED (generated didn't decisively beat the defender)."""
    win_margin = 0.05
    git_batcher = _build_git_batcher_with_reports(
        {
            "rounds/1/X/audit_submitted.json": _audit_report("submitted", "X", 0.0),  # passes D1
            "rounds/1/X/audit_leader.json": _audit_report("leader", "X", 0.02),  # below +W
        }
    )
    outcome = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="leader", win_margin=win_margin
    )
    assert outcome == VerificationOutcome.FAILED


async def test_compute_verification_re_derives_when_latest_defender_updates() -> None:
    """The verdict tracks `latest_defender` exactly — when it changes (e.g. timeline
    rejected and the miner re-climbed past a different opponent), `compute_verification`
    reads the D2 file matching the NEW defender. The OLD D2 against the previous defender
    sits on disk as a forensic artifact but is never consulted for the verdict.

    Concretely: X originally beat Y (D2-vs-Y on disk, would PASS). Timeline rejected,
    X re-climbed past 'leader' (latest_defender now 'leader'). audit_leader.json shows
    X.generated barely beats leader (below +W). The verdict re-derives to FAILED off
    the new D2, NOT off the still-on-disk audit_Y.json that would have said PASSED."""
    win_margin = 0.05
    git_batcher = _build_git_batcher_with_reports(
        {
            # D1 passes either way.
            "rounds/1/X/audit_submitted.json": _audit_report("submitted", "X", -0.02),
            # OLD D2 vs Y — PASSING signal, present on disk but should NOT be consulted
            # once latest_defender is no longer Y.
            "rounds/1/X/audit_Y.json": _audit_report("Y", "X", 0.50),
            # NEW D2 vs leader — below +W.
            "rounds/1/X/audit_leader.json": _audit_report("leader", "X", 0.02),
        }
    )

    # While latest_defender was Y, D2-vs-Y was the gate → PASSED.
    outcome_pre_reset = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="Y", win_margin=win_margin
    )
    assert outcome_pre_reset == VerificationOutcome.PASSED

    # After timeline reset → re-climb → latest_defender = "leader". The verdict re-derives
    # off audit_leader.json → FAILED. The PASSING audit_Y.json on disk is irrelevant.
    outcome_post_reset = await compute_verification(
        git_batcher=git_batcher, round_num=1, hotkey="X", latest_defender="leader", win_margin=win_margin
    )
    assert outcome_post_reset == VerificationOutcome.FAILED
