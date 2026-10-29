"""Retained Responses outcomes at the miner execution and validator boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import openai
import pytest
from miner_runtime.observations import Execution, ReplayMismatch
from miner_runtime.runner import run_generation
from miner_runtime.validator import ReplayWitness, run_replay
from openai.types.responses import Response
from test_recording import PRICING, SCOPE
from test_replay import Capture
from witness_client import WitnessClient
from witness_client.calls import RequestKey, canonical
from witness_client.client import CapturedResponse
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
        with pytest.raises(ReplayMismatch, match="Saved successful response is missing"):
            run_replay(generate, b"PNG", 42, replay, None)
        assert replay.budget.charges == {}
    else:
        assert run_replay(generate, b"PNG", 42, replay, None) == expected
    assert live.paid == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_event", ["response.created", "response.completed"])
def test_embedded_stream_error_allows_same_id_retry_without_charging_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool, error_event: str,
) -> None:
    pricing = canonical({**json.loads(PRICING), "calls_per_prompt": 1})
    live = Capture(tmp_path, pricing=pricing)
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": {"id": "fixture", "status": "in_progress"}},
        {"type": "response.completed", "response": {
            "id": "fixture", "status": "completed", "output": [], "usage": usage,
        }},
    ]
    for event in events:
        if event["type"] == error_event:
            event["error"] = {"message": "Fixture malformed stream"}
    failed_response = b"".join(b"data: " + canonical(event) + b"\n\n" for event in events)
    live.outcome = (200, failed_response, usage)
    capture_response = live._capture

    def capture_once(key: RequestKey, body: bytes, session_id: str | None) -> CapturedResponse:
        try:
            return capture_response(key, body, session_id)
        finally:
            live.outcome = None

    monkeypatch.setattr(live, "_capture", capture_once)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        try:
            result = (
                witness.create(model="fixture", input="image", request_id=7, stream=True)
                if stream
                else witness.create(model="fixture", input="image", request_id=7)
            )
            if stream:
                list(cast(ModelStream, result))
        except openai.APIError:
            return witness.create(model="fixture", input="image", request_id=7).output_text
        return witness.create(model="fixture", input="image", request_id=7).output_text

    expected = run_generation(generate, b"PNG", 42, live, None)
    assert expected == "7"
    assert live.create(model="fixture", input="image", request_id=7).output_text == expected
    located = live.records.locate(SCOPE, next(iter(live.captured)))
    assert located is not None
    key, _call = located
    execution = next(live._state.glob("executions/*/*/execution.json"))
    observed = Execution.model_validate_json(execution.read_bytes())
    original = next(entry.exchange_id for entry in observed.observations if entry.failure is not None)
    assert original is not None
    prefix = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{original}/"
    assert json.loads(live.state.get(prefix + "failure.json") or b"{}")["error_type"] == "InvalidProviderResponse"
    assert live.state.get(prefix + "response.sse") == failed_response
    assert live.budget.summary(SCOPE, key)["known_credits"] == "2.0"
    replay = ReplayWitness(live.export(tmp_path / "bundle"))
    assert run_replay(generate, b"PNG", 42, replay, None) == expected
    assert live.paid == 2 and len(replay.budget.charges) == 1
