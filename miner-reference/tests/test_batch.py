from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Unpack, cast

import httpx
import pytest
from bittensor_wallet import Keypair
from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from renderer_client import RenderOptions, RenderRejected, RenderUnavailable
from witness_client import WitnessClient, WitnessError
from witness_client.calls import RequestKey
from witness_client.client import CapturedResponse
from witness_client.pods import PodGatewayClient
from witness_client.request import serialize_body

from miner_reference.batch import BatchSettings, Prompt, run_batch
from miner_reference.batch import main as batch_main
from miner_reference.pipeline import Candidate, choose, normalize, run
from miner_reference.renders import SavedRenderer

PNG = b"\x89PNG\r\n\x1a\nfixture"


def result(text: str, model: str) -> Response:
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
    return response


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
        self.requests: list[tuple[int, ResponseCreateParamsBase]] = []
        self.invalid_choice = False
        self.failure: WitnessError | None = None

    def create(self, *, request_id: int = 0, **params: Unpack[ResponseCreateParamsBase]) -> Response:
        self.requests.append((request_id, params))
        if self.failure:
            raise self.failure
        model = str(params["model"])
        if request_id in (4, 7):
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
            text = f"export default function generate(THREE) {{ return new THREE.Group(); }} // {request_id}"
        return result(text, model)


class Witness(WitnessClient):
    def __init__(self) -> None:
        self._state = Path(tempfile.mkdtemp(prefix="miner-witness-"))
        self._mode = "api"
        self._config = {"round": "test-0", "witness_url": "https://witness.test", "proof_parameters": "unused"}
        self._http = cast(httpx.Client, SimpleNamespace(base_url="https://witness.test"))
        self._hotkey = cast(Keypair, SimpleNamespace(ss58_address="hotkey"))
        self._gateway = cast(PodGatewayClient, SimpleNamespace(delivery_timeout=5))
        self._calls_lock = threading.Lock()
        self._calls = {}
        self._call_slots = None
        self.recorded: dict[str, CapturedResponse] = {}
        self.lock = threading.Lock()
        self.active = self.peak = self.paid = 0
        self.first_pair: threading.Barrier | None = None
        self.lose_reply_once = False

    def _capture(self, key: RequestKey, body: bytes, session_id: str | None) -> CapturedResponse:
        with self.lock:
            saved = self.recorded.get(key.id)
        if saved is not None:
            return saved
        with self._call_slots if self._call_slots is not None else nullcontext():
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.paid += 1
                barrier = self.first_pair if self.first_pair is not None and self.paid <= self.first_pair.parties else None
            try:
                if barrier is not None:
                    barrier.wait(timeout=5)
                params = json.loads(body)
                response = Calls().create(request_id=key.witness_seed, **params)
                identifier = str(uuid.uuid4())
                (self._state / identifier).mkdir(mode=0o700)
                captured = CapturedResponse(identifier, 200, b"data: " + json.dumps({
                    "type": "response.completed", "response": response.model_dump(),
                }).encode() + b"\n\n")
                self._save_response(captured)
                with self.lock:
                    self.recorded[key.id] = captured
                    if self.lose_reply_once:
                        self.lose_reply_once = False
                        raise WitnessError("Lost delivery", identifier, source="gateway", code="connection_lost")
                return captured
            finally:
                with self.lock:
                    self.active -= 1


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
    return BatchSettings(
        tmp_path / "run", 42, "fixture",
        parallel_prompts=limits.get("parallel_prompts", 4),
        parallel_calls=limits.get("parallel_calls", 2),
        parallel_renders=limits.get("parallel_renders", 1),
    )


def test_five_candidates_refine_the_same_first_winner() -> None:
    calls, renderer = Calls(), Renderer()
    source = run(PNG, 42, calls, renderer)
    assert source in renderer.sources and len(renderer.sources) == 5
    requests = dict(calls.requests)
    assert [requests[i]["model"] for i in range(1, 8)] == ["gpt-6-astra"] + ["gpt-6.1-sol"] * 6
    assert len({seed for seed, _ in calls.requests}) == 7
    refinements = [json.loads(serialize_body({**requests[i], "stream": True}, "api"))["input"][0]["content"] for i in (5, 6)]
    assert refinements[0] == refinements[1]
    assert "widen the base" in refinements[0][2]["text"]
    assert any(value in refinements[0][2]["text"] for value in renderer.sources[:3])
    assert renderer.sources[3] not in refinements[1][2]["text"]


def test_initial_candidates_and_refinements_overlap_but_render_in_stable_order() -> None:
    initial, refinements = threading.Barrier(3), threading.Barrier(2)
    completed: set[int] = set()
    lock = threading.Lock()

    class ParallelCalls(Calls):
        def create(self, *, request_id: int = 0, **params: Unpack[ResponseCreateParamsBase]) -> Response:
            if request_id in (1, 2, 3):
                initial.wait(timeout=5)
            elif request_id in (5, 6):
                refinements.wait(timeout=5)
            with lock:
                if request_id == 4:
                    assert {1, 2, 3} <= completed
                elif request_id == 7:
                    assert {5, 6} <= completed
                completed.add(request_id)
            return super().create(request_id=request_id, **params)

    renderer = Renderer()
    assert run(PNG, 42, ParallelCalls(), renderer) is not None
    assert renderer.sources == [
        normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text) for i in (1, 2, 3, 5, 6)
    ]


def test_rejected_candidates_are_repaired_once_in_both_stages() -> None:
    calls, renderer = Calls(), Renderer()
    sources = {
        i: normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text) for i in range(1, 13)
    }
    renderer.reject = {sources[i] for i in (1, 2, 3, 5, 6)}
    assert run(PNG, 42, calls, renderer) in {sources[i] for i in (8, 9, 10, 11, 12)}
    assert renderer.sources == [sources[i] for i in (1, 2, 3, 8, 9, 10, 5, 6, 11, 12)]
    assert len(calls.requests) == 12
    requests = dict(calls.requests)
    for repair, original in ((8, 1), (9, 2), (10, 3), (11, 5), (12, 6)):
        request = json.loads(serialize_body({**requests[repair], "stream": True}, "api"))
        text = request["input"][0]["content"][-1]["text"]
        assert sources[original] in text and '{"error":"invalid_geometry"}' in text
        assert requests[repair]["model"] == "gpt-6.1-sol"


def test_failed_repairs_stop_without_another_inference() -> None:
    calls, renderer = Calls(), Renderer()
    renderer.reject = {
        normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text)
        for i in (1, 2, 3, 8, 9, 10)
    }
    assert run(PNG, 42, calls, renderer) is None
    assert len(calls.requests) == 6 and len(renderer.sources) == 6


def test_failed_refinement_repairs_keep_the_initial_winner() -> None:
    calls, renderer = Calls(), Renderer()
    renderer.reject = {
        normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text) for i in (5, 6, 11, 12)
    }
    assert run(PNG, 42, calls, renderer) in renderer.sources[:3]
    assert len(calls.requests) == 9


def test_invalid_selection_does_not_trigger_a_repair() -> None:
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
    assert len(list(witness._state.glob("executions/*/*/execution.json"))) == 4 and len(renderer.sources) == 20
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
    assert witness.paid == 10
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
    assert witness.paid == 10
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
    witness.first_pair = threading.Barrier(12)
    config = settings(tmp_path, parallel_prompts=8, parallel_calls=12, parallel_renders=2)
    statuses = run_batch(prompts, config, witness, renderer, progress=lambda _: None)
    assert len(statuses) == 128 and set(statuses.values()) == {"selected"}
    assert witness.paid == 896 and witness.peak == 12
    assert len(renderer.sources) == 640 and len(list(witness._state.glob("executions/*/*/execution.json"))) == 128


def test_check_command_validates_frozen_inputs_without_loading_credentials(
    tmp_path: Path,
    prompts: list[Prompt],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "witness.json"
    config.write_text(json.dumps({"round": "test-0"}))
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
            "--output",
            str(tmp_path / "run"),
            "--renderer-url",
            "http://localhost:18040",
            "--renderer-id",
            "fixture",
        ],
    )
    batch_main()
    checked = json.loads(capsys.readouterr().out)
    assert checked["successful_workflow_calls"] == 28
    assert checked["maximum_workflow_calls"] == 48
    assert checked["maximum_candidate_renders"] == 40
    assert not (tmp_path / "run").exists()
