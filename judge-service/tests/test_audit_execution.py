from unittest.mock import AsyncMock, MagicMock, patch

from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelWinner
from subnet_common.competition.verification_audit import VerificationOutcome
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.audit_execution import run_verification_audit


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
