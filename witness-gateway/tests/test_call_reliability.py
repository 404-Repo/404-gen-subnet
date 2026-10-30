"""Storage and replay fault cases using the established synthetic capture fixtures."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import openai
import pytest
from miner_runtime.observations import ReplayMismatch
from miner_runtime.runner import run_generation
from miner_runtime.validator import ReplayWitness, run_replay
from openai.types.responses import Response
from support import MemoryState
from test_recording import SCOPE
from test_recovery import PROOF, capture, evidence, prefix
from test_replay import Capture
from test_service import Harness, call_key
from witness_client import WitnessClient
from witness_client.calls import canonical
from witness_client.response import ModelStream

from pod_gateway.records import RecordingConflict
from pod_gateway.recovery import recover_issued


@pytest.mark.parametrize(
    "missing",
    [
        "manifest.json",
        "request.json",
        "response.sse",
        "client.bin",
        "server.bin",
        "authorization-server.bin",
        "claim.json",
        "session.json",
    ],
)
def test_missing_durable_capture_never_completes_or_permits_a_paid_retry(missing: str) -> None:
    state = MemoryState()
    records, budget, key, call = capture(state)
    for name, raw in evidence(budget, key, call).items():
        if name != missing:
            state.put(prefix(call) + name, raw)
    recovered = recover_issued(records, budget, state.read, SCOPE, key, call, PROOF)
    assert recovered.phase == "capturing"
    assert recovered.authorized
    assert budget.summary(SCOPE, key)["pending_usage"] == 1
    assert records.retry(SCOPE, key, call.exchange_id).exchange_id == call.exchange_id


@pytest.mark.parametrize(
    "field",
    [
        "call_id",
        "request_key",
        "round",
        "hotkey",
        "exchange_id",
        "pod_id",
        "pod_start_id",
        "proof_key_sha256",
        "pricing_sha256",
        "request_sha256",
        "response_sha256",
        "provider_status",
    ],
)
def test_wrong_durable_binding_cannot_settle_usage_or_allow_paid_retry(field: str) -> None:
    state = MemoryState()
    records, budget, key, call = capture(state)
    files = evidence(budget, key, call)
    manifest = json.loads(files["manifest.json"])
    manifest[field] = "different"
    files["manifest.json"] = canonical(manifest)
    for name, raw in files.items():
        state.put(prefix(call) + name, raw)
    with pytest.raises(RecordingConflict):
        recover_issued(records, budget, state.read, SCOPE, key, call, PROOF)
    assert budget.summary(SCOPE, key)["pending_usage"] == 1
    assert records.retry(SCOPE, key, call.exchange_id).exchange_id == call.exchange_id


@pytest.mark.parametrize("stream", [False, True])
def test_same_key_repeated_results_are_charged_once(
    tmp_path: Path,
    stream: bool,
) -> None:
    live = Capture(tmp_path)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        outcomes = []
        for _ in range(3):
            value = (
                witness.create(model="fixture", input="image", stream=True)
                if stream
                else witness.create(model="fixture", input="image")
            )
            if stream:
                outcomes.append("|".join(event.type for event in cast(ModelStream, value)))
            else:
                outcomes.append(cast(Response, value).output_text)
        return ":".join(outcomes)

    expected = run_generation(generate, b"PNG", 42, live, None)
    bundle = live.export(tmp_path / "bundle")
    assert live.paid == 1
    replay = ReplayWitness(bundle, timeout=0.2)
    assert run_replay(generate, b"PNG", 42, replay, None) == expected
    assert live.paid == 1


@pytest.mark.parametrize("failure", ["delivery", "timeout"])
def test_error_dependent_output_remains_a_mismatch(
    tmp_path: Path,
    failure: str,
) -> None:
    live = Capture(tmp_path)
    live.failures[0] = failure

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        outcomes = []
        for _ in range(2):
            try:
                outcomes.append(witness.create(model="fixture", input="image").output_text)
            except openai.APIConnectionError as error:
                outcomes.append(type(error).__name__)
        return ":".join(outcomes)

    expected = run_generation(generate, b"PNG", 42, live, None)
    assert expected == ("APITimeoutError:0" if failure == "timeout" else "APIConnectionError:0")
    bundle = live.export(tmp_path / "bundle")
    with pytest.raises(ReplayMismatch, match="Execution output differs"):
        run_replay(generate, b"PNG", 42, ReplayWitness(bundle), None)
    assert live.paid == 1


def test_many_independent_clients_join_one_capture_and_keep_the_same_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.allow_response.clear()
        async with fixture.running():
            with ExitStack() as stack, ThreadPoolExecutor(max_workers=32) as pool:
                clients = [stack.enter_context(fixture.client(f"client-{index}")) for index in range(32)]
                for client in clients:
                    client._gateway.delivery_timeout = 15
                    await asyncio.to_thread(client._gateway.ensure_pod)

                def create(index: int) -> tuple[str, int, int]:
                    client = clients[index]
                    if index % 2:
                        events = list(client.responses.create(model="fixture", input="image", stream=True))
                        assert events[-1].type == "response.completed"
                        response = events[-1].response
                    else:
                        response = client.responses.create(model="fixture", input="image")
                    assert response.usage is not None
                    return response.output_text, response.usage.input_tokens, response.usage.output_tokens

                loop = asyncio.get_running_loop()
                jobs = [loop.run_in_executor(pool, create, index) for index in range(32)]
                try:
                    assert await asyncio.to_thread(fixture.dispatched.wait, 5)
                    await asyncio.sleep(0.1)
                    assert fixture.paid == 1
                finally:
                    fixture.allow_response.set()
                results = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 25)
                assert results == [("answer-1", 10, 2)] * 32
                record = fixture.records.load(fixture.scope, call_key().id)
                assert record is not None and record.call.phase == "issued" and not record.attempts
                assert fixture.paid == fixture.provider.creates == 1

    asyncio.run(scenario())
