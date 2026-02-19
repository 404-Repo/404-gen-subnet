from unittest.mock import AsyncMock, patch

import pytest
from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelWinner, MatchReport
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.evaluate_duel import DuelResult
from judge_service.match_execution import run_match


def _gen(png: str | None = "https://cdn/preview.png", generation_time: float = 0) -> GenerationResult:
    """Shortcut to build a GenerationResult with sensible defaults."""
    return GenerationResult(glb="https://cdn/model.glb", png=png, generation_time=generation_time)


def _winners(report: MatchReport) -> dict[str, DuelWinner]:
    """Extract {duel_name: winner} from a report for easy assertion."""
    return {d.name: d.winner for d in report.duels}


async def _run(
    prompts: list[str],
    left_gens: dict[str, GenerationResult],
    right_gens: dict[str, GenerationResult],
    overtime_tolerance_ratio: float = 0.0,
    max_generation_time_seconds: float = 180,
) -> MatchReport:
    """Call run_match with fixed boilerplate args, only varying what matters per test."""
    return await run_match(
        openai=AsyncMock(),
        prompts=prompts,
        seed=42,
        left_gens=left_gens,
        right_gens=right_gens,
        left="left_hk",
        right="right_hk",
        max_concurrent_duels=4,
        overtime_tolerance_ratio=overtime_tolerance_ratio,
        max_generation_time_seconds=max_generation_time_seconds,
        shutdown=GracefulShutdown(),
    )


@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_aggregates_scores_into_margin(mock_eval: AsyncMock) -> None:
    """All three VLM outcomes (LEFT/DRAW/RIGHT) flow through to score and margin."""
    mock_eval.side_effect = [
        DuelResult(outcome=-1, issues="left better"),
        DuelResult(outcome=0, issues="tied"),
        DuelResult(outcome=1, issues="right better"),
        DuelResult(outcome=1, issues="right better"),
    ]

    gens = {s: _gen() for s in "abcd"}
    report = await _run(["a.png", "b.png", "c.png", "d.png"], left_gens=gens, right_gens=gens)

    assert _winners(report) == {
        "a": DuelWinner.LEFT,
        "b": DuelWinner.DRAW,
        "c": DuelWinner.RIGHT,
        "d": DuelWinner.RIGHT,
    }
    assert report.score == 1  # -1 + 0 + 1 + 1
    assert report.margin == 0.25


@pytest.mark.parametrize(
    "left_png, right_png, expected_winner",
    [
        (None, "https://cdn/p.png", DuelWinner.RIGHT),  # left missing
        ("https://cdn/p.png", None, DuelWinner.LEFT),  # right missing
        (None, None, DuelWinner.DRAW),  # both missing
    ],
)
@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_missing_preview_auto_resolves(
    mock_eval: AsyncMock, left_png: str | None, right_png: str | None, expected_winner: DuelWinner
) -> None:
    """Missing preview auto-wins for the opponent; no VLM call wasted."""
    mock_eval.return_value = DuelResult(outcome=0, issues="draw")

    left_gens = {"chair": _gen(), "table": _gen(png=left_png)}
    right_gens = {"chair": _gen(), "table": _gen(png=right_png)}

    report = await _run(["chair.png", "table.png"], left_gens=left_gens, right_gens=right_gens)

    assert _winners(report) == {"chair": DuelWinner.DRAW, "table": expected_winner}
    assert mock_eval.call_count == 1


@pytest.mark.parametrize(
    "left_time, right_time, expected_winner",
    [
        (200, 60, DuelWinner.RIGHT),  # left penalized
        (60, 200, DuelWinner.LEFT),  # right penalized
        (200, 200, DuelWinner.DRAW),  # both penalized
    ],
)
@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_overtime_penalty(
    mock_eval: AsyncMock, left_time: float, right_time: float, expected_winner: DuelWinner
) -> None:
    """Overtime beyond tolerance auto-resolves without a VLM call."""
    mock_eval.return_value = DuelResult(outcome=0, issues="draw")

    left_gens = {"chair": _gen(), "table": _gen(generation_time=left_time)}
    right_gens = {"chair": _gen(), "table": _gen(generation_time=right_time)}

    report = await _run(
        ["chair.png", "table.png"],
        left_gens=left_gens,
        right_gens=right_gens,
        max_generation_time_seconds=180,
    )

    assert _winners(report) == {"chair": DuelWinner.DRAW, "table": expected_winner}
    assert mock_eval.call_count == 1


@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_overtime_tolerates_first_n_then_penalizes(mock_eval: AsyncMock) -> None:
    """The first overtime prompt within tolerance goes to VLM; the rest are auto-penalized."""
    mock_eval.return_value = DuelResult(outcome=0, issues="draw")

    prompts = ["a.png", "b.png", "c.png", "d.png"]
    left_gens = {s: _gen() for s in "abcd"}
    right_gens = {
        "a": _gen(generation_time=200),  # overtime (1st — tolerated)
        "b": _gen(generation_time=60),  # normal
        "c": _gen(generation_time=200),  # overtime (2nd — penalized)
        "d": _gen(generation_time=60),  # normal
    }

    report = await _run(
        prompts,
        left_gens=left_gens,
        right_gens=right_gens,
        overtime_tolerance_ratio=0.25,  # allowance = int(4 * 0.25) = 1
        max_generation_time_seconds=180,
    )

    assert _winners(report) == {"a": DuelWinner.DRAW, "b": DuelWinner.DRAW, "c": DuelWinner.LEFT, "d": DuelWinner.DRAW}
    assert mock_eval.call_count == 3


@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_shutdown_skips_pending_duels(mock_eval: AsyncMock) -> None:
    """Shutdown mid-match: in-flight duel completes, pending duels return SKIPPED."""
    shutdown = GracefulShutdown()

    async def shutdown_after_first_eval(*args, **kwargs):
        shutdown.request_shutdown()
        return DuelResult(outcome=1, issues="right wins")

    mock_eval.side_effect = shutdown_after_first_eval

    gens = {"chair": _gen(), "table": _gen()}

    report = await run_match(
        openai=AsyncMock(),
        prompts=["chair.png", "table.png"],
        seed=42,
        left_gens=gens,
        right_gens=gens,
        left="left_hk",
        right="right_hk",
        max_concurrent_duels=1,  # sequential: first completes and triggers shutdown
        overtime_tolerance_ratio=0.0,
        max_generation_time_seconds=180,
        shutdown=shutdown,
    )

    assert _winners(report) == {"chair": DuelWinner.RIGHT, "table": DuelWinner.SKIPPED}
    assert mock_eval.call_count == 1
