from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Unpack

import pytest
from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming
from renderer_client import RenderOptions, RenderRejected, RenderUnavailable
from witness_client import WitnessClient, WitnessError, WitnessResult
from witness_client.calls import Generation, RequestKey
from witness_client.client import GenerationWitness
from witness_client.request import serialize_body

from miner_reference.batch import BatchSettings, Prompt, run_batch
from miner_reference.batch import main as batch_main
from miner_reference.pipeline import Candidate, choose, run
from miner_reference.renders import SavedRenderer

PNG = b"\x89PNG\r\n\x1a\nfixture"


def result(text: str, model: str) -> WitnessResult:
    response = Response.model_validate(
        {
            "id": "resp_fixture",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": model,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "output": [
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    )
    return WitnessResult(
        "hotkey",
        text,
        {"input_tokens": 10, "output_tokens": 20},
        response=response,
        _exchange_id="fixture",
    )


class Renderer:
    renderer_id = "fixture-renderer"

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.reject: set[str] = set()
        self.unavailable = 0

    def render(self, source: str, options: RenderOptions = RenderOptions()) -> bytes:
        if self.unavailable:
            self.unavailable -= 1
            raise RenderUnavailable("Temporary render outage")
        self.sources.append(source)
        if source in self.reject:
            raise RenderRejected('{"error":"invalid_geometry"}')
        return PNG + source.encode()


class Calls:
    def __init__(self) -> None:
        self.requests: list[tuple[int, ResponseCreateParamsStreaming]] = []
        self.invalid_choice = False
        self.failure: WitnessError | None = None

    def create(self, *, witness_seed: int = 0, **params: Unpack[ResponseCreateParamsStreaming]) -> WitnessResult:
        self.requests.append((witness_seed, params))
        if self.failure:
            raise self.failure
        model = str(params["model"])
        if witness_seed in (4, 7):
            text = (
                "not JSON"
                if self.invalid_choice
                else json.dumps(
                    {
                        "winner": "A",
                        "reason": "closest silhouette",
                        "improvements": "widen the base",
                    }
                )
            )
        else:
            text = f"export default function generate(THREE) {{ return new THREE.Group(); }} // {witness_seed}"
        return result(text, model)


class Witness(WitnessClient):
    def __init__(self) -> None:
        self.requests: dict[str, list[RequestKey]] = {}
        self.recorded: dict[tuple[str, str], WitnessResult] = {}
        self.finished: dict[str, str | None] = {}
        self.lock = threading.Lock()
        self.active = self.peak = self.paid = 0
        self.first_pair: threading.Barrier | None = None
        self.lose_reply_once = False

    def for_generation(
        self,
        image: bytes,
        seed: int,
        source_sha256: str,
        *,
        prompt_url: str | None = None,
    ) -> GenerationWitness:
        generation = Generation.from_prompt(image, seed, source_sha256, round_number="test-0", prompt_url=prompt_url)
        parent = self

        class Bound(GenerationWitness):
            def __init__(self) -> None:
                self.generation = generation
                self.responses = self
                self.position = 0

            def create(
                self, *, witness_seed: int = 0, **params: Unpack[ResponseCreateParamsStreaming]
            ) -> WitnessResult:
                key = RequestKey.from_body("api", serialize_body(params, "api"), witness_seed)
                with parent.lock:
                    requests = parent.requests.setdefault(generation.id, [])
                    if self.position < len(requests):
                        assert requests[self.position] == key, "Generation request order changed on resume"
                    else:
                        requests.append(key)
                    self.position += 1
                    saved = parent.recorded.get((generation.id, key.id))
                    if saved is not None:
                        return saved
                    parent.active += 1
                    parent.peak = max(parent.peak, parent.active)
                    parent.paid += 1
                    barrier = parent.first_pair if parent.paid <= 2 else None
                try:
                    if barrier is not None:
                        barrier.wait(timeout=5)
                    value = Calls().create(witness_seed=witness_seed, **params)
                    with parent.lock:
                        parent.recorded[generation.id, key.id] = value
                        if parent.lose_reply_once:
                            parent.lose_reply_once = False
                            raise WitnessError("Lost delivery", source="gateway", code="connection_lost")
                    return value
                finally:
                    with parent.lock:
                        parent.active -= 1

            def finish(self, output: str | None) -> None:
                with parent.lock:
                    parent.finished[generation.id] = output

        return Bound()


@pytest.fixture
def prompts(tmp_path: Path) -> list[Prompt]:
    values = []
    for i in range(4):
        path = tmp_path / f"{i}.png"
        image = PNG + bytes([i])
        path.write_bytes(image)
        values.append(Prompt(str(i), path, hashlib.sha256(image).hexdigest()))
    return values


def settings(tmp_path: Path, **limits: int) -> BatchSettings:
    return BatchSettings(tmp_path / "run", 42, "a" * 64, "fixture", **limits)


def test_five_candidates_refine_the_same_first_winner() -> None:
    calls, renderer = Calls(), Renderer()
    source = run(PNG, 42, calls, renderer)
    assert source in renderer.sources and len(renderer.sources) == 5
    assert [params["model"] for _, params in calls.requests] == ["gpt-6-astra"] + ["gpt-6.1-sol"] * 6
    assert len({seed for seed, _ in calls.requests}) == 7
    refinements = [
        json.loads(serialize_body(params, "api"))["input"][0]["content"] for _, params in calls.requests[4:6]
    ]
    assert refinements[0] == refinements[1]
    assert "widen the base" in refinements[0][2]["text"]
    assert any(value in refinements[0][2]["text"] for value in renderer.sources[:3])
    assert renderer.sources[3] not in refinements[1][2]["text"]


def test_invalid_candidates_and_selections_do_not_trigger_repairs() -> None:
    calls, renderer = Calls(), Renderer()
    renderer.reject = {Calls().create(witness_seed=i, stream=True, model="gpt-6.1-sol").output_text for i in (1, 2, 3)}
    assert run(PNG, 42, calls, renderer) is None
    assert len(calls.requests) == 3
    malformed = Calls()
    malformed.invalid_choice = True
    assert choose(PNG, 42, malformed, [Candidate("source", PNG)], 4) is None
    assert len(malformed.requests) == 1


def test_infrastructure_failures_propagate_instead_of_changing_the_algorithm() -> None:
    calls = Calls()
    calls.failure = WitnessError("Gateway unavailable", source="gateway")
    with pytest.raises(WitnessError):
        run(PNG, 42, calls, Renderer())


def test_batch_runs_prompts_in_parallel_with_a_shared_call_limit(tmp_path: Path, prompts: list[Prompt]) -> None:
    witness, renderer = Witness(), Renderer()
    witness.first_pair = threading.Barrier(2)
    config = settings(tmp_path, parallel_prompts=4, parallel_calls=2)
    assert run_batch(prompts, config, witness, renderer, progress=lambda _: None) == {
        str(i): "selected" for i in range(4)
    }
    assert witness.peak == 2 and witness.paid == 28
    assert len(witness.finished) == 4 and len(renderer.sources) == 20
    assert len(list(config.output.glob("*/selected.js"))) == 4
    assert run_batch(prompts, config, witness, renderer, progress=lambda _: None) == {
        str(i): "selected" for i in range(4)
    }
    assert witness.paid == 28 and len(renderer.sources) == 20


def test_render_outage_does_not_block_other_prompts_or_repeat_paid_results(
    tmp_path: Path, prompts: list[Prompt]
) -> None:
    witness, renderer = Witness(), Renderer()
    renderer.unavailable = 1
    config = settings(tmp_path, parallel_prompts=1)
    first = run_batch(prompts[:2], config, witness, renderer, progress=lambda _: None)
    assert first == {"0": "pending", "1": "selected"}
    assert witness.paid == 8
    second = run_batch(prompts[:2], config, witness, renderer, progress=lambda _: None)
    assert second == {"0": "selected", "1": "selected"}
    assert witness.paid == 14


def test_saved_render_rejection_is_stable_and_png_tampering_is_rejected(tmp_path: Path) -> None:
    renderer, slots = Renderer(), threading.BoundedSemaphore(1)
    renderer.reject.add("invalid")
    with pytest.raises(RenderRejected):
        SavedRenderer(renderer, tmp_path, slots).render("invalid")
    renderer.reject.clear()
    with pytest.raises(RenderRejected):
        SavedRenderer(renderer, tmp_path, slots).render("invalid")
    assert renderer.sources == ["invalid"]
    directory = tmp_path / "valid"
    SavedRenderer(renderer, directory, slots).render("valid")
    (directory / "00.png").write_bytes(PNG + b"changed")
    with pytest.raises(ValueError, match="PNG differs"):
        SavedRenderer(renderer, directory, slots).render("valid")


def test_lost_model_reply_recovers_the_exact_prompt_and_request(tmp_path: Path, prompts: list[Prompt]) -> None:
    witness, renderer = Witness(), Renderer()
    witness.lose_reply_once = True
    config = settings(tmp_path, parallel_prompts=1)
    assert run_batch(prompts[:2], config, witness, renderer, progress=lambda _: None) == {
        "0": "pending",
        "1": "selected",
    }
    assert witness.paid == 8
    assert run_batch(prompts[:2], config, witness, renderer, progress=lambda _: None) == {
        "0": "selected",
        "1": "selected",
    }
    assert witness.paid == 14


def test_batch_rejects_changed_inputs_and_output(tmp_path: Path, prompts: list[Prompt]) -> None:
    witness, renderer = Witness(), Renderer()
    config = settings(tmp_path)
    run_batch(prompts[:1], config, witness, renderer, progress=lambda _: None)
    with pytest.raises(ValueError, match="identity changed"):
        run_batch(prompts[1:2], config, witness, renderer, progress=lambda _: None)
    (config.output / "0/selected.js").write_text("changed")
    assert run_batch(prompts[:1], config, witness, renderer, progress=lambda _: None) == {"0": "pending"}
    assert witness.paid == 7


def test_128_prompt_batch_finishes_896_calls_and_640_renders(tmp_path: Path) -> None:
    prompts = []
    for i in range(128):
        image = PNG + str(i).encode()
        path = tmp_path / f"{i}.png"
        path.write_bytes(image)
        prompts.append(Prompt(str(i), path, hashlib.sha256(image).hexdigest()))
    witness, renderer = Witness(), Renderer()
    witness.first_pair = threading.Barrier(2)
    statuses = run_batch(prompts, settings(tmp_path), witness, renderer, progress=lambda _: None)
    assert len(statuses) == 128 and set(statuses.values()) == {"selected"}
    assert witness.paid == 896 and witness.peak == 2
    assert len(renderer.sources) == 640 and len(witness.finished) == 128


def test_check_command_validates_frozen_inputs_without_loading_credentials(
    tmp_path: Path,
    prompts: list[Prompt],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "witness.json"
    config.write_text(json.dumps({"transport": "pods-v1", "round": "test-0"}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "seed": 42,
                "prompts": [{"id": p.id, "path": str(p.path), "sha256": p.sha256} for p in prompts],
            }
        )
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch",
            "check",
            "--manifest",
            str(manifest),
            "--witness-config",
            str(config),
            "--source-sha256",
            "a" * 64,
            "--output",
            str(tmp_path / "run"),
            "--renderer-url",
            "http://localhost:18040",
            "--renderer-id",
            "fixture",
        ],
    )
    batch_main()
    assert json.loads(capsys.readouterr().out)["successful_workflow_calls"] == 28
    assert not (tmp_path / "run").exists()
