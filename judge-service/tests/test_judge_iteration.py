from unittest.mock import AsyncMock, patch

import pytest
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

from judge_service.judge_iteration import judge_iteration
from judge_service.settings import Settings


def _add_state(git: MockGitHubClient, stage: RoundStage, round_num: int = 1) -> None:
    state = CompetitionState(current_round=round_num, stage=stage)
    git.files["state.json"] = state.model_dump_json(indent=2)


@pytest.mark.parametrize(
    "stage",
    [s for s in RoundStage if s != RoundStage.DUELS],
)
async def test_non_duels_stage_is_noop(settings: Settings, stage: RoundStage) -> None:
    """judge_iteration returns immediately when the stage is not DUELS."""
    git = MockGitHubClient()
    _add_state(git, stage)

    with patch("judge_service.judge_iteration.MatchRunner") as mock_runner_cls:
        await judge_iteration(git=git, openai=AsyncMock(), settings=settings, shutdown=GracefulShutdown())

    mock_runner_cls.create.assert_not_called()
    assert git.committed == {}


async def test_duels_stage_creates_and_runs_match_runner(settings: Settings) -> None:
    """judge_iteration creates a MatchRunner and calls run() when in DUELS stage."""
    git = MockGitHubClient()
    _add_state(git, RoundStage.DUELS)

    mock_runner = AsyncMock()
    mock_create = AsyncMock(return_value=mock_runner)

    with patch("judge_service.judge_iteration.MatchRunner") as mock_runner_cls:
        mock_runner_cls.create = mock_create
        shutdown = GracefulShutdown()
        await judge_iteration(git=git, openai=AsyncMock(), settings=settings, shutdown=shutdown)

    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["state"].stage == RoundStage.DUELS
    assert call_kwargs["settings"] is settings

    mock_runner.run.assert_called_once_with(shutdown)
