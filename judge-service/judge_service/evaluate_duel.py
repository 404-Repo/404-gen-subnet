import asyncio
from typing import Literal

import httpx
from loguru import logger
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, Field
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_chain,
    wait_fixed,
    wait_random,
)


MODEL = "zai-org/GLM-4.1V-9B-Thinking"
TEMPERATURE = 0.0
MAX_TOKENS = 1024
SYSTEM_PROMPT = """
You are a specialized 3D model evaluation system. 
Analyze visual quality and prompt adherence with expert precision. 
Always respond with valid JSON only."""
USER_PROMPT_IMAGE = """Does each 3D model match the image prompt?

Penalty 0-10:
0 = Perfect match
3 = Minor issues (slight shape differences, missing small details)
5 = Moderate issues (wrong style, significant details missing)
7 = Major issues (wrong category but related, e.g. chair vs stool)
10 = Completely wrong object

Output: {"penalty_1": <0-10>, "penalty_2": <0-10>, "issues": "<brief>"}"""


class JudgeResponse(BaseModel):
    """Response from a judge evaluating a duel between two models."""

    penalty_1: int
    """Penalty for the first model (0-10, lower is better)."""
    penalty_2: int
    """Penalty for the second model (0-10, lower is better)."""
    issues: str
    """Human-readable issue summary produced by the judge."""


class DuelResult(BaseModel):
    """Result of a position-balanced duel evaluation."""

    outcome: Literal[-1, 0, 1] = Field(..., description="Duel outcome: -1 = left wins, 0 = draw, 1 = right wins")
    issues: str = Field(..., description="Human-readable issue summary from judge")


def _is_retryable(exc: BaseException) -> bool:
    """Check if the exception is retryable (transient errors only)."""
    if isinstance(exc, APIConnectionError | httpx.ConnectError | httpx.ReadTimeout):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (429, 500, 502, 503, 504):
        return True
    return False


def _log_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(f"Retrying ask_judge (attempt {retry_state.attempt_number}): {exc}")


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_chain(wait_fixed(10), wait_fixed(30)) + wait_random(0, 5),
    before_sleep=_log_retry,
)
async def ask_judge(
    client: AsyncOpenAI,
    prompt_url: str,
    left_url: str,
    right_url: str,
    seed: int,
) -> JudgeResponse:
    """Single VLM call to compare two 3D models against a prompt."""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image prompt to generate 3D model:"},
                {
                    "type": "image_url",
                    "image_url": {"url": prompt_url},
                },
                {"type": "text", "text": "First 3D model (4 different views):"},
                {
                    "type": "image_url",
                    "image_url": {"url": left_url},
                },
                {"type": "text", "text": "Second 3D model (4 different views):"},
                {
                    "type": "image_url",
                    "image_url": {"url": right_url},
                },
                {"type": "text", "text": USER_PROMPT_IMAGE},
            ],
        },
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "judge-response",
            "schema": JudgeResponse.model_json_schema(),
        },
    }

    completion = await client.chat.completions.create(
        model=MODEL,
        messages=messages,  # type: ignore
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        response_format=response_format,  # type: ignore
        seed=seed,
    )

    return JudgeResponse.model_validate_json(completion.choices[0].message.content)


async def evaluate_duel(client: AsyncOpenAI, prompt_url: str, left_url: str, right_url: str, seed: int) -> DuelResult:
    """Run a position-balanced duel (two calls with swapped order) and return an outcome."""

    try:
        result_1, result_2 = await asyncio.gather(
            ask_judge(client, prompt_url, left_url, right_url, seed),
            ask_judge(client, prompt_url, right_url, left_url, seed),
        )
        left_penalty = (result_1.penalty_1 + result_2.penalty_2) / 2
        right_penalty = (result_1.penalty_2 + result_2.penalty_1) / 2

        if abs(left_penalty - right_penalty) <= 1:
            outcome: Literal[-1, 0, 1] = 0
        elif left_penalty < right_penalty:
            outcome = -1
        else:
            outcome = 1

        return DuelResult(
            outcome=outcome,
            issues=result_1.issues,
        )
    except Exception as e:
        logger.exception(f"Failed to evaluate duel: {e}")
        return DuelResult(outcome=0, issues="Internal error")
