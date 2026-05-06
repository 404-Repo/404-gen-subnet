from unittest.mock import AsyncMock, patch

import pytest
from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelWinner, MatchReport
from subnet_common.graceful_shutdown import GracefulShutdown

from judge_service.match_execution import run_match


def _gen(views: str | None = "https://cdn/preview") -> GenerationResult:
    """Shortcut to build a GenerationResult with sensible defaults."""
    return GenerationResult(js="https://cdn/model.js", views=views)


def _winners(report: MatchReport) -> dict[str, DuelWinner]:
    return {d.name: d.winner for d in report.duels}


async def _run(
    prompts: list[str],
    left_gens: dict[str, GenerationResult],
    right_gens: dict[str, GenerationResult],
) -> MatchReport:
    return await run_match(
        openai=AsyncMock(),
        prompts=prompts,
        seed=42,
        left_gens=left_gens,
        right_gens=right_gens,
        left="left_hk",
        right="right_hk",
        max_concurrent_vlm_calls=4,
        max_concurrent_duels=4,
        shutdown=GracefulShutdown(),
    )


@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_aggregates_scores_into_margin(mock_judge: AsyncMock) -> None:
    """All three judge outcomes (LEFT/DRAW/RIGHT) flow through to score and margin."""
    mock_judge.side_effect = [
        (DuelWinner.LEFT, {"stages": []}),
        (DuelWinner.DRAW, {"stages": []}),
        (DuelWinner.RIGHT, {"stages": []}),
        (DuelWinner.RIGHT, {"stages": []}),
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
    "left_views, right_views, expected_winner",
    [
        (None, "https://cdn/p", DuelWinner.RIGHT),  # left missing
        ("https://cdn/p", None, DuelWinner.LEFT),  # right missing
        (None, None, DuelWinner.DRAW),  # both missing
    ],
)
@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_missing_preview_auto_resolves(
    mock_judge: AsyncMock, left_views: str | None, right_views: str | None, expected_winner: DuelWinner
) -> None:
    """The judge handles preview-missing internally — verify it gets called and returns reasonable winners."""

    # For the missing-preview prompt, simulate the judge's own preview-existence gate.
    def _side_effect(*_args, **kwargs):
        left = kwargs["left_gen"]
        right = kwargs["right_gen"]
        if not left.views and not right.views:
            return DuelWinner.DRAW, {"reason": "no previews"}
        if not left.views:
            return DuelWinner.RIGHT, {"reason": "no preview from left"}
        if not right.views:
            return DuelWinner.LEFT, {"reason": "no preview from right"}
        return DuelWinner.DRAW, {"stages": []}

    mock_judge.side_effect = _side_effect

    left_gens = {"chair": _gen(), "table": _gen(views=left_views)}
    right_gens = {"chair": _gen(), "table": _gen(views=right_views)}

    report = await _run(["chair.png", "table.png"], left_gens=left_gens, right_gens=right_gens)

    assert _winners(report) == {"chair": DuelWinner.DRAW, "table": expected_winner}


@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_shutdown_skips_pending_duels(mock_judge: AsyncMock) -> None:
    """Shutdown before evaluation: each duel returns SKIPPED."""
    shutdown = GracefulShutdown()
    shutdown.request_shutdown()

    mock_judge.return_value = (DuelWinner.LEFT, {"stages": []})

    gens = {"chair": _gen(), "table": _gen()}

    report = await run_match(
        openai=AsyncMock(),
        prompts=["chair.png", "table.png"],
        seed=42,
        left_gens=gens,
        right_gens=gens,
        left="left_hk",
        right="right_hk",
        max_concurrent_vlm_calls=1,
        max_concurrent_duels=2,
        shutdown=shutdown,
    )

    assert _winners(report) == {"chair": DuelWinner.SKIPPED, "table": DuelWinner.SKIPPED}
    mock_judge.assert_not_called()


@patch("judge_service.match_execution.evaluate_duel", new_callable=AsyncMock)
async def test_run_match_persists_detail(mock_judge: AsyncMock) -> None:
    """The judge detail dict (slim per-stage scores) round-trips onto DuelReport.detail."""
    fake_detail = {
        "decided_by": "S2 consensus",
        "s1": {"choice": "draw", "n_total": 4, "n_consistent": 0, "angles": []},
        "s2": {"choice": "B", "source": "consensus_3"},
        "s4": {"choice": "B", "side_verdicts": {"a": "ok", "b": "ok"}},
    }
    mock_judge.return_value = (DuelWinner.RIGHT, fake_detail)

    gens = {"chair": _gen()}
    report = await _run(["chair.png"], left_gens=gens, right_gens=gens)

    assert report.duels[0].winner == DuelWinner.RIGHT
    assert report.duels[0].detail == fake_detail
