from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import pytest
from support import MemoryState
from witness_client import ReplayMismatch, ReplayWitness
from witness_client.calls import Generation, Invocation, RequestKey, canonical
from witness_client.request import serialize_body

from pod_gateway.models import Scope, object_dict
from pod_gateway.records import CallRecords
from pod_gateway.replay import export_bundle


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    state, scope = MemoryState(), Scope("competition", "test-0", "5" * 48)
    records = CallRecords(state)
    generation = Generation.from_prompt(b"PNG", 42, "a" * 64, round_number="test-0")
    body = serialize_body(
        {
            "stream": True,
            "model": "fixture",
            "input": "image",
            "service_tier": "default",
        },
        "api",
    )
    invocations = [
        Invocation(
            generation=generation,
            ordinal=i,
            request=RequestKey.from_body("api", body, i),
        )
        for i in range(2)
    ]
    for invocation, text in zip(invocations, ("first", "second"), strict=True):
        call = records.reserve(scope, invocation)
        assigned = records.assign(
            scope, invocation, "00000000-0000-4000-8000-000000000001", "pod-one"
        )
        assert records.claim(scope, invocation, "pod-one", assigned.capture_id or "")
        records.authorize(scope, invocation, "pod-one")
        response = (
            b"data: "
            + canonical(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": text}],
                            }
                        ],
                    },
                }
            )
            + b"\n\n"
        )
        metadata = {
            "generation_id": generation.id,
            "call_id": invocation.request.id,
            "request_key": invocation.request.model_dump(),
            "round": scope.round,
            "hotkey": scope.hotkey,
            "exchange_id": call.exchange_id,
            "proof_key_sha256": "c" * 64,
            "pricing_sha256": "d" * 64,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "provider_status": 200,
        }
        prefix = f"rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
        for name, value in {
            "manifest.json": canonical(metadata),
            "request.json": body,
            "response.sse": response,
            "session.json": b"{}",
            "client.bin": b"fixture",
            "server.bin": b"fixture",
            "authorization-server.bin": b"fixture",
            "claim.json": b"{}",
        }.items():
            state.put(prefix + name, value)
        records.issued(scope, invocation, "pod-one", canonical(metadata))
    records.deliver(scope, invocations[1])
    records.deliver(scope, invocations[0])
    records.finish(scope, generation, 2, "source")

    def verify(_: str, directory: Path) -> dict[str, object]:
        return object_dict(json.loads((directory / "manifest.json").read_bytes()))

    output = tmp_path / "bundle"
    export_bundle(
        records=records,
        evidence=state,
        scope=scope,
        generation_id=generation.id,
        output=output,
        proof_hash="c" * 64,
        pricing_hash="d" * 64,
        verify=verify,
    )
    return output


def test_replay_restores_different_concurrent_calls_and_their_delivery_order(
    bundle: Path,
) -> None:
    replay = ReplayWitness(bundle, timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        second = pool.submit(
            replay.create, stream=True, model="fixture", input="image", witness_seed=1
        )
        with pytest.raises(FutureTimeout):
            second.result(timeout=0.1)
        first = pool.submit(replay.create, stream=True, model="fixture", input="image")
        assert second.result(timeout=5).output_text == "second"
        assert first.result(timeout=5).output_text == "first"
    replay.finish("source")


def test_replay_rejects_changed_and_unconsumed_calls(bundle: Path) -> None:
    replay = ReplayWitness(bundle)
    with pytest.raises(ReplayMismatch):
        replay.create(stream=True, model="fixture", input="different image")
    incomplete = ReplayWitness(bundle)
    with pytest.raises(ReplayMismatch):
        incomplete.finish("source")


def test_replay_rejects_response_tampering_after_verification(bundle: Path) -> None:
    replay = ReplayWitness(bundle)
    first = replay.recording.calls[replay.recording.entries[0].call_id]
    (bundle / first.exchange_id / "response.sse").write_bytes(b"altered")
    with pytest.raises(ReplayMismatch):
        replay.create(stream=True, model="fixture", input="image")
