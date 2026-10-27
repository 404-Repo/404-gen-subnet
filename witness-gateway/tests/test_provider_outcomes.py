"""Retained Responses outcomes at the miner execution and validator boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import openai
import pytest
from miner_runtime.accounting import BudgetRejected
from miner_runtime.runner import run_generation
from miner_runtime.validator import ReplayWitness, run_replay
from openai.types.responses import Response
from test_replay import Capture
from witness_client import WitnessClient
from witness_client.calls import canonical
from witness_client.response import ModelStream


@pytest.mark.parametrize(
    "outcome", ["empty", "refusal", "structured", "failed", "incomplete", "sse-error", "missing-usage"]
)
@pytest.mark.parametrize("stream", [False, True])
def test_retained_outcomes_replay_exact_events_errors_text_and_usage(
    tmp_path: Path,
    outcome: str,
    stream: bool,
) -> None:
    live = Capture(tmp_path)
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    status = outcome if outcome in ("failed", "incomplete") else "completed"
    text = '{"ok":true}' if outcome == "structured" else "retained text"
    content = (
        [{"type": "refusal", "refusal": "Synthetic fixture refusal"}]
        if outcome == "refusal"
        else []
        if outcome == "empty"
        else [{"type": "output_text", "text": text}]
    )
    response: dict[str, object] = {
        "id": "fixture",
        "status": status,
        "output": [{"type": "message", "content": content}],
        "usage": None if outcome == "missing-usage" else usage,
    }
    if outcome == "failed":
        response["error"] = {"code": "server_error", "message": "Fixture failure"}
    if outcome == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": {"id": "fixture", "status": "in_progress"}}
    ]
    if outcome == "sse-error":
        events.append({"type": "error", "code": "fixture_stream_error", "message": "Fixture stream failed"})
    else:
        events.append({"type": "response." + status, "response": response})
    raw = b"".join(b"data: " + canonical(event) + b"\n\n" for event in events)
    live.outcome = (200, raw, None if outcome in ("missing-usage", "sse-error") else usage)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        try:
            result = (
                witness.create(model="fixture", input="image", stream=True)
                if stream
                else witness.create(model="fixture", input="image")
            )
            if stream:
                seen = []
                try:
                    for event in cast(ModelStream, result):
                        seen.append(event.model_dump(mode="json"))
                except openai.APIError as error:
                    seen.append({"sdk_error": type(error).__name__, "code": error.code})
                return json.dumps(seen, sort_keys=True)
            return json.dumps(cast(Response, result).model_dump(mode="json"), sort_keys=True)
        except openai.APIError as error:
            return f"{type(error).__name__}:{error.code}:{error.message}"

    expected = run_generation(generate, b"PNG", 42, live, None)
    replay = ReplayWitness(live.export(tmp_path / "bundle"))
    if outcome in ("missing-usage", "sse-error"):
        assert generate(b"PNG", 42, replay, None) == expected
        with pytest.raises(BudgetRejected, match="unknown usage"):
            replay.finish(expected)
    else:
        assert run_replay(generate, b"PNG", 42, replay, None) == expected
    assert live.paid == 1
