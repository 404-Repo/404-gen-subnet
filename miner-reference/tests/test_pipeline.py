from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Unpack

import httpx
import pytest
from bittensor_wallet import Keypair
from miner_runtime import batch
from openai import APIConnectionError, APIError, APITimeoutError, DefaultHttpxClient
from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from renderer_client import RenderOptions, RenderRejected
from witness_client import AsyncWitnessClient, WitnessError
from witness_client.request import serialize_body

from main import generate
from miner_reference.pipeline import Candidate, choose, normalize, run

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

    def render(self, source: str, options: RenderOptions = RenderOptions()) -> bytes:
        self.sources.append(source)
        if source in self.reject:
            raise RenderRejected('{"error":"invalid_geometry"}')
        return PNG + source.encode()


class Calls:
    def __init__(self) -> None:
        self.requests: list[tuple[int, ResponseCreateParamsBase]] = []
        self.invalid_choice = False
        self.failure: WitnessError | APIError | None = None

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


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def renderer() -> Renderer:
    return Renderer()


@pytest.fixture
def fake_io(monkeypatch: pytest.MonkeyPatch, calls: Calls, renderer: Renderer) -> None:
    async def create(
        self: AsyncWitnessClient, *, request_id: int = 0, **params: Unpack[ResponseCreateParamsBase]
    ) -> Response:
        return calls.create(request_id=request_id, **params)

    async def render(self: AsyncWitnessClient, source: str, options: RenderOptions = RenderOptions()) -> bytes:
        return renderer.render(source, options)

    monkeypatch.setattr(AsyncWitnessClient, "create", create)
    monkeypatch.setattr(AsyncWitnessClient, "render", render)


@pytest.fixture
def witness(tmp_path: Path, fake_io: None) -> Iterator[AsyncWitnessClient]:
    client = AsyncWitnessClient(
        {"witness_url": "https://witness.test", "proof_parameters": str(tmp_path)},
        hotkey=Keypair.create_from_uri("//Alice"), openai_api_key="synthetic",
    )
    yield client
    asyncio.run(client.aclose())


def test_five_candidates_refine_the_same_first_winner(
    witness: AsyncWitnessClient, calls: Calls, renderer: Renderer,
) -> None:
    source = asyncio.run(run(PNG, 42, witness))
    assert source in renderer.sources and len(renderer.sources) == 5
    requests = dict(calls.requests)
    assert [requests[i]["model"] for i in range(1, 8)] == ["gpt-6-astra"] + ["gpt-6.1-sol"] * 6
    assert len({seed for seed, _ in calls.requests}) == 7
    refinements = [json.loads(serialize_body({**requests[i], "stream": True}, "api"))["input"][0]["content"] for i in (5, 6)]
    assert refinements[0] == refinements[1]
    assert "widen the base" in refinements[0][2]["text"]
    assert any(value in refinements[0][2]["text"] for value in renderer.sources[:3])
    assert renderer.sources[3] not in refinements[1][2]["text"]


def test_initial_candidates_and_refinements_overlap_but_render_in_stable_order(
    witness: AsyncWitnessClient, calls: Calls, renderer: Renderer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial, refinements = asyncio.Barrier(3), asyncio.Barrier(2)
    completed: set[int] = set()

    async def create(*, request_id: int = 0, **params: Unpack[ResponseCreateParamsBase]) -> Response:
        if request_id in (1, 2, 3):
            await asyncio.wait_for(initial.wait(), 5)
        elif request_id in (5, 6):
            await asyncio.wait_for(refinements.wait(), 5)
        if request_id == 4:
            assert {1, 2, 3} <= completed
        elif request_id == 7:
            assert {5, 6} <= completed
        completed.add(request_id)
        return calls.create(request_id=request_id, **params)

    monkeypatch.setattr(witness.responses, "create", create)
    assert asyncio.run(run(PNG, 42, witness)) is not None
    assert renderer.sources == [
        normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text) for i in (1, 2, 3, 5, 6)
    ]


def test_rejected_candidates_are_repaired_once_in_both_stages(
    witness: AsyncWitnessClient, calls: Calls, renderer: Renderer,
) -> None:
    sources = {
        i: normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text) for i in range(1, 13)
    }
    renderer.reject = {sources[i] for i in (1, 2, 3, 5, 6)}
    assert asyncio.run(run(PNG, 42, witness)) in {sources[i] for i in (8, 9, 10, 11, 12)}
    assert renderer.sources == [sources[i] for i in (1, 2, 3, 8, 9, 10, 5, 6, 11, 12)]
    assert len(calls.requests) == 12
    requests = dict(calls.requests)
    for repair, original in ((8, 1), (9, 2), (10, 3), (11, 5), (12, 6)):
        request = json.loads(serialize_body({**requests[repair], "stream": True}, "api"))
        text = request["input"][0]["content"][-1]["text"]
        assert sources[original] in text and '{"error":"invalid_geometry"}' in text
        assert requests[repair]["model"] == "gpt-6.1-sol"


def test_failed_repairs_stop_without_another_inference(
    witness: AsyncWitnessClient, calls: Calls, renderer: Renderer,
) -> None:
    renderer.reject = {
        normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text)
        for i in (1, 2, 3, 8, 9, 10)
    }
    assert asyncio.run(run(PNG, 42, witness)) is None
    assert len(calls.requests) == 6 and len(renderer.sources) == 6


def test_failed_refinement_repairs_keep_the_initial_winner(
    witness: AsyncWitnessClient, calls: Calls, renderer: Renderer,
) -> None:
    renderer.reject = {
        normalize(Calls().create(request_id=i, model="gpt-6.1-sol").output_text) for i in (5, 6, 11, 12)
    }
    assert asyncio.run(run(PNG, 42, witness)) in renderer.sources[:3]
    assert len(calls.requests) == 9


def test_invalid_selection_does_not_trigger_a_repair(witness: AsyncWitnessClient, calls: Calls) -> None:
    calls.invalid_choice = True
    assert asyncio.run(choose(PNG, 42, witness, [Candidate("source", PNG)], 4)) is None
    assert len(calls.requests) == 1


@pytest.mark.parametrize("selecting", [False, True])
@pytest.mark.parametrize("failure", ["gateway", "connection", "timeout"])
def test_infrastructure_failures_propagate_instead_of_changing_the_algorithm(
    witness: AsyncWitnessClient, calls: Calls, selecting: bool, failure: str,
) -> None:
    if failure == "gateway":
        error: WitnessError | APIError = WitnessError("Gateway unavailable", source="gateway")
    else:
        with DefaultHttpxClient() as http:
            request = http.build_request("POST", "https://api.openai.com/v1/responses")
        error = (APIConnectionError if failure == "connection" else APITimeoutError)(request=request)
    calls.failure = error
    with pytest.raises(type(error)):
        if selecting:
            asyncio.run(choose(PNG, 42, witness, [Candidate("source", PNG)], 4))
        else:
            asyncio.run(run(PNG, 42, witness))


def test_128_prompt_batch_finishes_896_calls_and_640_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_io: None, calls: Calls, renderer: Renderer,
) -> None:
    config = tmp_path / "witness.json"
    config.write_text(json.dumps({"witness_url": "https://witness.test", "proof_parameters": str(tmp_path)}))
    prompts = {}
    for i in range(128):
        image = PNG + str(i).encode()
        prompts[hashlib.sha256(image).hexdigest()] = image
    submitted: dict[str, dict[str, object]] = {}

    async def prover(self: AsyncWitnessClient) -> None:
        pass

    async def read_saved(http: httpx.AsyncClient) -> set[str]:
        return set(submitted)

    async def submit(self: AsyncWitnessClient, image: bytes, source: str | None) -> dict[str, object]:
        assert source is not None
        value: dict[str, object] = {"source": source}
        submitted[hashlib.sha256(image).hexdigest()] = value
        return value

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(batch, "Wallet", lambda **kwargs: SimpleNamespace(hotkey=Keypair.create_from_uri("//Alice")))
    monkeypatch.setattr(AsyncWitnessClient, "_get_prover", prover)
    monkeypatch.setattr(AsyncWitnessClient, "submit", submit)

    async def scenario() -> None:
        await batch.run(
            generate, prompts, 42, witness_config=config, read_saved=read_saved, workers=8, attempts=1, api=True
        )
        assert set(submitted) == set(prompts)
        assert len(calls.requests) == 896 and len(renderer.sources) == 640
        await batch.run(
            generate, prompts, 42, witness_config=config, read_saved=read_saved, workers=8, attempts=1, api=True
        )
        assert len(calls.requests) == 896 and len(renderer.sources) == 640

    asyncio.run(scenario())
