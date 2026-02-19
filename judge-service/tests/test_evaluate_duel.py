from unittest.mock import AsyncMock, MagicMock, patch

from judge_service.evaluate_duel import (
    DuelResult,
    JudgeResponse,
    ask_judge,
    evaluate_duel,
)


PROMPT_URL = "https://cdn.example.com/prompt.png"
LEFT_URL = "https://cdn.example.com/left.png"
RIGHT_URL = "https://cdn.example.com/right.png"
SEED = 42


def _make_completion(response: JudgeResponse) -> MagicMock:
    """Build a mock ChatCompletion containing a serialised JudgeResponse."""
    choice = MagicMock()
    choice.message.content = response.model_dump_json()
    completion = MagicMock()
    completion.choices = [choice]
    return completion


def _make_openai_client(responses: list[JudgeResponse]) -> AsyncMock:
    """Build an AsyncOpenAI mock that returns completions for each response in sequence."""
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_make_completion(r) for r in responses])
    return client


async def test_ask_judge_parses_response() -> None:
    expected = JudgeResponse(penalty_1=2, penalty_2=5, issues="Minor shape difference")
    client = _make_openai_client([expected])

    result = await ask_judge(client=client, prompt_url=PROMPT_URL, left_url=LEFT_URL, right_url=RIGHT_URL, seed=SEED)

    assert result == expected


@patch("judge_service.evaluate_duel.ask_judge", new_callable=AsyncMock)
async def test_evaluate_duel_left_wins(mock_ask: AsyncMock) -> None:
    """The left model is clearly better (lower penalty)."""
    mock_ask.side_effect = [
        # Normal order: penalty_1 -> left, penalty_2 -> right
        JudgeResponse(penalty_1=0, penalty_2=8, issues="Left perfect"),
        # Swapped order: penalty_1 -> right, penalty_2 -> left
        JudgeResponse(penalty_1=8, penalty_2=0, issues="Right bad"),
    ]
    # left_penalty = (0 + 0) / 2 = 0
    # right_penalty = (8 + 8) / 2 = 8

    result = await evaluate_duel(
        client=AsyncMock(), prompt_url=PROMPT_URL, left_url=LEFT_URL, right_url=RIGHT_URL, seed=SEED
    )

    assert result.outcome == -1
    assert result.issues == "Left perfect"


@patch("judge_service.evaluate_duel.ask_judge", new_callable=AsyncMock)
async def test_evaluate_duel_right_wins(mock_ask: AsyncMock) -> None:
    """Right model is clearly better (lower penalty)."""
    mock_ask.side_effect = [
        JudgeResponse(penalty_1=8, penalty_2=0, issues="Right perfect"),
        JudgeResponse(penalty_1=0, penalty_2=8, issues="Left bad"),
    ]
    # left_penalty = (8 + 8) / 2 = 8
    # right_penalty = (0 + 0) / 2 = 0

    result = await evaluate_duel(
        client=AsyncMock(), prompt_url=PROMPT_URL, left_url=LEFT_URL, right_url=RIGHT_URL, seed=SEED
    )

    assert result.outcome == 1


@patch("judge_service.evaluate_duel.ask_judge", new_callable=AsyncMock)
async def test_evaluate_duel_draw_at_threshold_boundary(mock_ask: AsyncMock) -> None:
    """Penalty difference of exactly 1.0 is still a draw (a threshold is <= 1)."""
    mock_ask.side_effect = [
        JudgeResponse(penalty_1=2, penalty_2=4, issues="Close"),
        JudgeResponse(penalty_1=3, penalty_2=3, issues="Close"),
    ]
    # left_penalty = (2 + 3) / 2 = 2.5
    # right_penalty = (4 + 3) / 2 = 3.5
    # abs(2.5 - 3.5) = 1.0 <= 1 -> draw

    result = await evaluate_duel(
        client=AsyncMock(), prompt_url=PROMPT_URL, left_url=LEFT_URL, right_url=RIGHT_URL, seed=SEED
    )

    assert result.outcome == 0


@patch("judge_service.evaluate_duel.ask_judge", new_callable=AsyncMock)
async def test_evaluate_duel_swaps_positions_for_balancing(mock_ask: AsyncMock) -> None:
    """The second ask_judge call has left/right URLs swapped for position balancing."""
    mock_ask.return_value = JudgeResponse(penalty_1=3, penalty_2=3, issues="OK")

    await evaluate_duel(client=AsyncMock(), prompt_url=PROMPT_URL, left_url=LEFT_URL, right_url=RIGHT_URL, seed=SEED)

    assert mock_ask.call_count == 2
    first_call, second_call = mock_ask.call_args_list

    assert first_call.args[2] == LEFT_URL
    assert first_call.args[3] == RIGHT_URL

    assert second_call.args[2] == RIGHT_URL
    assert second_call.args[3] == LEFT_URL


@patch("judge_service.evaluate_duel.ask_judge", new_callable=AsyncMock)
async def test_evaluate_duel_exception_returns_safe_draw(mock_ask: AsyncMock) -> None:
    """Any exception from ask_judge is caught and produces a safe draw."""
    mock_ask.side_effect = RuntimeError("VLM crashed")

    result = await evaluate_duel(
        client=AsyncMock(), prompt_url=PROMPT_URL, left_url=LEFT_URL, right_url=RIGHT_URL, seed=SEED
    )

    assert result == DuelResult(outcome=0, issues="Internal error")
