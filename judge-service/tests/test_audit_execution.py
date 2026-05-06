from unittest.mock import AsyncMock, MagicMock, patch

from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelWinner, MatchReport
from subnet_common.competition.verification_audit import VerificationOutcome
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.audit_execution import run_informational_audit_duel, run_verification_audit


def _gen(views: str | None = "https://cdn/preview") -> GenerationResult:
    return GenerationResult(js="https://cdn/model.js", views=views)


async def _run(winner_seq: list[DuelWinner], prompts: list[str]) -> tuple:
    """Helper: mock evaluate_duel to return the given winners in order; run audit; return (audit, save_calls)."""
    save_calls: list = []

    async def _save(git_batcher, round_num, report):  # noqa: ARG001
        save_calls.append(report)

    git_batcher = MagicMock()

    submitted = {p: _gen() for p in prompts}
    generated = {p: _gen() for p in prompts}

    with (
        patch(
            "judge_service.audit_execution.evaluate_duel",
            new_callable=AsyncMock,
            side_effect=[(w, {"stages": []}) for w in winner_seq],
        ),
        patch("judge_service.audit_execution.save_match_report", new=_save),
    ):
        audit = await run_verification_audit(
            openai=AsyncMock(),
            git_batcher=git_batcher,
            round_num=1,
            hotkey="hk1",
            prompts=[f"{p}.png" for p in prompts],
            seed=42,
            submitted_gens=submitted,
            generated_gens=generated,
            max_concurrent_vlm_calls=2,
            max_concurrent_duels=2,
            shutdown=GracefulShutdown(),
        )
    return audit, save_calls


async def test_failed_when_submitted_dominates() -> None:
    """All submitted (LEFT) wins → score = -N, FAILED. Submitted dominating means
    the miner can't reproduce their submission with their own solution — suspicious."""
    audit, saved = await _run(
        [DuelWinner.LEFT, DuelWinner.LEFT, DuelWinner.LEFT],
        prompts=["a", "b", "c"],
    )
    assert audit.outcome == VerificationOutcome.FAILED
    assert audit.score == -3
    assert audit.checked_prompts == 3
    assert len(saved) == 1
    # Match report saved with left="submitted" so persistence path is `duels_submitted.json`.
    # Matches per-duel left_js=submitted, mirroring miner-vs-miner file convention.
    assert saved[0].left == "submitted"
    assert saved[0].right == "hk1"


async def test_passed_when_generated_dominates() -> None:
    """All generated (RIGHT) wins → score = +N, PASSED. Generated holding up means
    the solution can reproduce or exceed the submission — legitimate."""
    audit, _ = await _run(
        [DuelWinner.RIGHT, DuelWinner.RIGHT, DuelWinner.RIGHT],
        prompts=["a", "b", "c"],
    )
    assert audit.outcome == VerificationOutcome.PASSED
    assert audit.score == 3
    assert audit.checked_prompts == 3


async def test_passed_on_draw() -> None:
    """All draws → score = 0, still PASSED (threshold accepts ties).

    `checked_prompts` includes draws — a duel the judge ruled DRAW was still checked,
    just inconclusive. Only judge-side crashes (SKIPPED) are excluded.
    """
    audit, _ = await _run(
        [DuelWinner.DRAW, DuelWinner.DRAW, DuelWinner.DRAW],
        prompts=["a", "b", "c"],
    )
    assert audit.outcome == VerificationOutcome.PASSED
    assert audit.score == 0
    assert audit.checked_prompts == 3
    assert audit.drawn == 3
    assert audit.total_prompts == 3


async def test_passed_when_mixed_but_nonnegative() -> None:
    """LEFT, RIGHT, RIGHT → -1 + 1 + 1 = +1 → PASSED."""
    audit, _ = await _run(
        [DuelWinner.LEFT, DuelWinner.RIGHT, DuelWinner.RIGHT],
        prompts=["a", "b", "c"],
    )
    assert audit.outcome == VerificationOutcome.PASSED
    assert audit.score == 1
    assert audit.checked_prompts == 3


async def test_failed_when_submitted_wins_overall() -> None:
    """LEFT, RIGHT, LEFT → -1 + 1 - 1 = -1 → FAILED. Net submitted-wins is the cheating signal."""
    audit, _ = await _run(
        [DuelWinner.LEFT, DuelWinner.RIGHT, DuelWinner.LEFT],
        prompts=["a", "b", "c"],
    )
    assert audit.outcome == VerificationOutcome.FAILED
    assert audit.score == -1
    assert audit.checked_prompts == 3


async def test_skipped_prompts_count_as_zero() -> None:
    """Skipped duels (e.g. preview missing) count as 0 — neither side gets credit."""
    audit, _ = await _run(
        [DuelWinner.SKIPPED, DuelWinner.RIGHT, DuelWinner.SKIPPED],
        prompts=["a", "b", "c"],
    )
    assert audit.outcome == VerificationOutcome.PASSED
    assert audit.score == 1  # only the RIGHT win contributed
    assert audit.checked_prompts == 1


# Informational audit: audited.generated vs the defender they beat. No verdict.


async def test_informational_audit_saves_to_distinct_path_with_correct_sides() -> None:
    """Persists at `rounds/{n}/{audited}/audit_vs_{defender[:10]}.json`. Defender is LEFT
    so positive margin (RIGHT wins) reads as 'audited.generated still beats the defender' —
    same legitimacy direction as the verification audit's submitted-vs-generated."""
    write_calls: list[dict] = []

    async def _write(*, path: str, content: str, message: str) -> None:
        write_calls.append({"path": path, "content": content, "message": message})

    git_batcher = MagicMock()
    git_batcher.write = AsyncMock(side_effect=_write)

    captured_kwargs: dict = {}

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        captured_kwargs.update(kwargs)
        # Caller passes left=defender, right=audited; surface that on the report.
        return MatchReport(
            left=str(kwargs["left"]),
            right=str(kwargs["right"]),
            score=2,
            margin=0.5,
            duels=[],
        )

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        report = await run_informational_audit_duel(
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

    # run_match got the right side assignment.
    assert captured_kwargs["left"] == "defender_full_hotkey_yyy"
    assert captured_kwargs["right"] == "audited_full_hotkey_xxxxx"

    # Saved exactly once, at the audit-specific path (not `duels_*.json`, so it can't
    # collide with the original miner-vs-miner match between this pair).
    assert len(write_calls) == 1
    assert write_calls[0]["path"] == "rounds/7/audited_full_hotkey_xxxxx/audit_defender_f.json"
    assert "Informational audit" in write_calls[0]["message"]
    assert report.margin == 0.5


async def test_informational_audit_skips_save_on_shutdown() -> None:
    """Shutdown set before save → returns the report but doesn't persist (next iteration
    won't re-trigger this audit; informational data is best-effort)."""
    git_batcher = MagicMock()
    git_batcher.write = AsyncMock()
    shutdown = GracefulShutdown()

    async def _fake_run_match(**kwargs: object) -> MatchReport:
        shutdown.request_shutdown()
        return MatchReport(left=str(kwargs["left"]), right=str(kwargs["right"]), score=0, margin=0.0, duels=[])

    with patch("judge_service.audit_execution.run_match", new=_fake_run_match):
        await run_informational_audit_duel(
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
