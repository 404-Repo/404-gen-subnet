"""Tests for judge_service.evaluate_duel module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from judge_service.evaluate_duel import (
    DuelResult,
    JudgeResponse,
    _is_retryable,
    ask_judge,
    evaluate_duel,
)


# ---------------------------------------------------------------------------
# JudgeResponse model tests
# ---------------------------------------------------------------------------


class TestJudgeResponse:
    """Validate JudgeResponse Pydantic model constraints."""

    def test_valid_response(self) -> None:
        resp = JudgeResponse(penalty_1=3, penalty_2=7, issues="minor shape diff")
        assert resp.penalty_1 == 3
        assert resp.penalty_2 == 7
        assert resp.issues == "minor shape diff"

    def test_zero_penalties(self) -> None:
        resp = JudgeResponse(penalty_1=0, penalty_2=0, issues="")
        assert resp.penalty_1 == 0
        assert resp.penalty_2 == 0

    def test_max_penalties(self) -> None:
        resp = JudgeResponse(penalty_1=10, penalty_2=10, issues="completely wrong")
        assert resp.penalty_1 == 10
        assert resp.penalty_2 == 10

    def test_json_roundtrip(self) -> None:
        resp = JudgeResponse(penalty_1=5, penalty_2=2, issues="style mismatch")
        json_str = resp.model_dump_json()
        restored = JudgeResponse.model_validate_json(json_str)
        assert restored == resp

    def test_schema_has_required_fields(self) -> None:
        schema = JudgeResponse.model_json_schema()
        assert "penalty_1" in schema["properties"]
        assert "penalty_2" in schema["properties"]
        assert "issues" in schema["properties"]


# ---------------------------------------------------------------------------
# DuelResult model tests
# ---------------------------------------------------------------------------


class TestDuelResult:
    """Validate DuelResult Pydantic model."""

    @pytest.mark.parametrize("outcome", [-1, 0, 1])
    def test_valid_outcomes(self, outcome: int) -> None:
        result = DuelResult(outcome=outcome, issues="test")
        assert result.outcome == outcome

    def test_invalid_outcome_rejected(self) -> None:
        with pytest.raises(Exception):
            DuelResult(outcome=2, issues="bad")

    def test_issues_field(self) -> None:
        result = DuelResult(outcome=0, issues="Internal error")
        assert result.issues == "Internal error"


# ---------------------------------------------------------------------------
# _is_retryable tests
# ---------------------------------------------------------------------------


class TestIsRetryable:
    """Test the retry predicate for transient errors."""

    def test_api_connection_error_is_retryable(self) -> None:
        exc = APIConnectionError(request=MagicMock())
        assert _is_retryable(exc) is True

    def test_httpx_connect_error_is_retryable(self) -> None:
        exc = httpx.ConnectError("connection refused")
        assert _is_retryable(exc) is True

    def test_httpx_read_timeout_is_retryable(self) -> None:
        exc = httpx.ReadTimeout("read timed out")
        assert _is_retryable(exc) is True

    @pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
    def test_retryable_status_codes(self, status_code: int) -> None:
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = {}
        exc = APIStatusError(
            message="error",
            response=mock_response,
            body=None,
        )
        assert _is_retryable(exc) is True

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
    def test_non_retryable_status_codes(self, status_code: int) -> None:
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = {}
        exc = APIStatusError(
            message="error",
            response=mock_response,
            body=None,
        )
        assert _is_retryable(exc) is False

    def test_generic_exception_not_retryable(self) -> None:
        assert _is_retryable(ValueError("bad value")) is False

    def test_runtime_error_not_retryable(self) -> None:
        assert _is_retryable(RuntimeError("oops")) is False


# ---------------------------------------------------------------------------
# evaluate_duel tests
# ---------------------------------------------------------------------------


class TestEvaluateDuel:
    """Test position-balanced duel evaluation logic."""

    @staticmethod
    def _make_client_returning(responses: list[JudgeResponse]) -> AsyncMock:
        """Create a mock OpenAI client that returns pre-defined JudgeResponses."""
        client = AsyncMock()
        call_count = 0

        async def fake_create(**kwargs):
            nonlocal call_count
            resp = responses[call_count % len(responses)]
            call_count += 1
            mock_completion = MagicMock()
            mock_completion.choices = [MagicMock()]
            mock_completion.choices[0].message.content = resp.model_dump_json()
            return mock_completion

        client.chat.completions.create = fake_create
        return client

    @pytest.mark.asyncio
    async def test_left_wins_clearly(self) -> None:
        """Left model has much lower penalty -> outcome -1."""
        # Call 1 (normal order): left=0, right=8
        # Call 2 (swapped): left(now right)=0, right(now left)=8
        # left_penalty = (0 + 0) / 2 = 0
        # right_penalty = (8 + 8) / 2 = 8
        responses = [
            JudgeResponse(penalty_1=0, penalty_2=8, issues="left is better"),
            JudgeResponse(penalty_1=8, penalty_2=0, issues="right is better"),
        ]
        client = self._make_client_returning(responses)
        result = await evaluate_duel(client, "prompt.png", "left.png", "right.png", seed=42)
        assert result.outcome == -1

    @pytest.mark.asyncio
    async def test_right_wins_clearly(self) -> None:
        """Right model has much lower penalty -> outcome 1."""
        # Call 1 (normal): left=8, right=0
        # Call 2 (swapped): left(now right)=8, right(now left)=0
        # left_penalty = (8 + 8) / 2 = 8
        # right_penalty = (0 + 0) / 2 = 0
        responses = [
            JudgeResponse(penalty_1=8, penalty_2=0, issues="right is better"),
            JudgeResponse(penalty_1=0, penalty_2=8, issues="left is better"),
        ]
        client = self._make_client_returning(responses)
        result = await evaluate_duel(client, "prompt.png", "left.png", "right.png", seed=42)
        assert result.outcome == 1

    @pytest.mark.asyncio
    async def test_draw_when_equal(self) -> None:
        """Equal penalties -> outcome 0 (draw)."""
        responses = [
            JudgeResponse(penalty_1=5, penalty_2=5, issues="similar quality"),
            JudgeResponse(penalty_1=5, penalty_2=5, issues="similar quality"),
        ]
        client = self._make_client_returning(responses)
        result = await evaluate_duel(client, "prompt.png", "left.png", "right.png", seed=42)
        assert result.outcome == 0

    @pytest.mark.asyncio
    async def test_draw_within_tolerance(self) -> None:
        """Difference <= 1 is a draw."""
        # Call 1: left=4, right=5 -> diff of 1
        # Call 2 (swapped): penalty_1=5, penalty_2=4
        # left_penalty = (4 + 4) / 2 = 4
        # right_penalty = (5 + 5) / 2 = 5
        # diff = 1 -> draw
        responses = [
            JudgeResponse(penalty_1=4, penalty_2=5, issues="close"),
            JudgeResponse(penalty_1=5, penalty_2=4, issues="close"),
        ]
        client = self._make_client_returning(responses)
        result = await evaluate_duel(client, "prompt.png", "left.png", "right.png", seed=42)
        assert result.outcome == 0

    @pytest.mark.asyncio
    async def test_exception_returns_draw(self) -> None:
        """Any exception during evaluation returns a draw with error message."""
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
        result = await evaluate_duel(client, "prompt.png", "left.png", "right.png", seed=42)
        assert result.outcome == 0
        assert result.issues == "Internal error"
