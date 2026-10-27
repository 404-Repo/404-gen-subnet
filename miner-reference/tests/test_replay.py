"""Reproduce the complete reference pipeline through the execution boundary."""

import hashlib
from pathlib import Path

import httpx
import pytest
from miner_runtime.observations import Execution, ReplayMismatch
from miner_runtime.runner import run_generation
from miner_runtime.validator import ReplayWitness, run_replay
from renderer_client import RecordingRenderer, ReplayRenderer
from renderer_client import Renderer as RendererProtocol
from test_batch import PNG, Calls, Renderer, Witness
from witness_client import WitnessClient
from witness_client.calls import canonical
from witness_client.pods import CallView

from miner_reference.pipeline import normalize, run


@pytest.mark.parametrize("repair", [False, True])
def test_complete_pipeline_reconstructs_from_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair: bool) -> None:
    live = Witness()
    renderer = Renderer()
    if repair:
        renderer.reject = {
            normalize(Calls().create(request_id=i, model="fixture").output_text) for i in (1, 2, 3, 5, 6)
        }
    previews = RecordingRenderer(renderer)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: RendererProtocol) -> str | None:
        with pytest.raises(ReplayMismatch, match="live runner"):
            witness.submit(image, None)
        with pytest.raises(ReplayMismatch, match="live runner"):
            witness.get_submitted()
        return run(image, seed, witness.responses, renderer)

    expected = run_generation(generate, PNG, 42, live, previews)
    assert expected is not None and live.paid == (12 if repair else 7)
    execution = next(live._state.glob("executions/*/*/execution.json")).read_bytes()
    captures = {}
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for step in Execution.model_validate_json(execution).observations:
        captured = live.recorded[step.request.id]
        directory = bundle / captured.exchange_id
        directory.mkdir(exist_ok=True)
        manifest = canonical(
            {
                "request_key": step.request.model_dump(),
                "call_id": step.request.id,
                "provider_status": 200,
                "response_sha256": hashlib.sha256(captured.body).hexdigest(),
            }
        )
        (directory / "manifest.json").write_bytes(manifest)
        (directory / "response.sse").write_bytes(captured.body)
        call = CallView(
            request=step.request,
            exchange_id=captured.exchange_id,
            deadline=2000000000,
            phase="issued",
            authorized=True,
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        )
        captures[captured.exchange_id] = {
            "call": call.model_dump(),
            "model": "fixture",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    pricing = canonical(
        {
            "calls_per_prompt": 20,
            "credits_per_prompt": 1000,
            "long_context_above_input_tokens": 272000,
            "models": {"fixture": {"input_credits_per_token": 1, "output_credits_per_token": 1}},
        }
    )
    (bundle / "execution.json").write_bytes(execution)
    (bundle / "pricing.json").write_bytes(pricing)
    (bundle / "verified.json").write_bytes(
        canonical(
            {
                "version": 2,
                "execution_sha256": hashlib.sha256(execution).hexdigest(),
                "pricing_sha256": hashlib.sha256(pricing).hexdigest(),
                "captures": captures,
            }
        )
    )
    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Validator replay must not contact a live Pod")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    for _ in range(2):
        replay = ReplayWitness(bundle)
        rendered = ReplayRenderer(previews.records, renderer_id=previews.renderer_id)
        assert run_replay(generate, PNG, 42, replay, rendered) == expected
        rendered.assert_consumed()
    assert live.paid == (12 if repair else 7)
