from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from support import MemoryState
from witness_client import ReplayMismatch, ReplayWitness, WitnessClient, WitnessError
from witness_client.calls import Generation, Invocation, RequestKey, canonical
from witness_client.client import GenerationWitness
from witness_client.pods import CallView, PodGatewayClient, RecordingView, Snapshot
from witness_client.request import serialize_body
from witness_client.types import Session

from pod_gateway.models import Scope, object_dict
from pod_gateway.records import CallRecords
from pod_gateway.replay import export_bundle


@pytest.fixture(params=["prompt", "legacy-source"])
def bundle(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    state, scope = MemoryState(), Scope("competition", "test-0", "5" * 48)
    records = CallRecords(state)
    generation = Generation.from_prompt(b"PNG", 42, round_number="test-0")
    if request.param == "legacy-source":
        generation = Generation.model_validate({**generation.model_dump(), "source_sha256": "a" * 64})
        assert generation.id == "94877b97856e5ac7c071b78168ee47e20d620a89f07a6268e9eb792bd450bf9f"
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
        records.authorize(scope, invocation, "pod-one", exchange_id=call.exchange_id)
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
        records.issued(scope, invocation, "pod-one", canonical(metadata), exchange_id=call.exchange_id)
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


@pytest.mark.parametrize("legacy", [False, True])
def test_replay_restores_different_concurrent_calls_and_their_delivery_order(
    bundle: Path, legacy: bool,
) -> None:
    if legacy:
        recording = json.loads((bundle / "recording.json").read_bytes())
        for call in recording["calls"].values():
            call.pop("not_dispatched")
        raw = canonical(recording)
        (bundle / "recording.json").write_bytes(raw)
        verified = json.loads((bundle / "verified.json").read_bytes())
        verified["recording_sha256"] = hashlib.sha256(raw).hexdigest()
        (bundle / "verified.json").write_bytes(canonical(verified))
    replay = ReplayWitness(bundle, timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        second = pool.submit(
            replay.create, stream=True, model="fixture", input="image", request_id=1
        )
        with pytest.raises(FutureTimeout):
            second.result(timeout=0.1)
        first = pool.submit(replay.responses.create, model="fixture", input="image")
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


def resumed_generation(
    bundle: Path, timeout: float = 5, call_slots: threading.Semaphore | None = None
) -> GenerationWitness:
    recording = RecordingView.model_validate_json((bundle / "recording.json").read_bytes())

    class CachedGateway(PodGatewayClient):
        def __init__(self) -> None:
            self.delivery_timeout = timeout

        def recording(self, generation: Generation) -> RecordingView:
            return recording

        def reserve(self, invocation: Invocation) -> Snapshot:
            call = recording.calls[recording.entries[invocation.ordinal].call_id]
            # Completed captures remain reusable after their original deadline.
            return Snapshot(call=call.model_copy(update={"deadline": 1}), round="test-0", hotkey="5" * 48, session=None)

        def recorded(self, invocation: Invocation, call: CallView, proof_hash: str,
                     **kwargs: object) -> tuple[Session, int, bytes]:
            session: Session = {
                "id": call.exchange_id, "miner": "5" * 48, "round": "test-0", "protocol": 2,
                "proof_key_sha256": proof_hash, "provider": "https://api.openai.com/v1/responses",
            }
            return session, 200, (bundle / call.exchange_id / "response.sse").read_bytes()

        def delivered(self, invocation: Invocation) -> int:
            delivery = recording.entries[invocation.ordinal].delivery
            assert delivery is not None
            return delivery

    state = bundle.parent / "resumed"
    state.mkdir(mode=0o700)
    parent = cast(
        WitnessClient,
        SimpleNamespace(
            _config={}, _mode="api", _api_key="fixture", _state=state,
            _prover=SimpleNamespace(verifying_key_sha256="c" * 64),
            _preparation=threading.Lock(),
            _hotkey=SimpleNamespace(ss58_address="5" * 48), _http=None, _gateway=CachedGateway(),
        ),
    )
    return GenerationWitness(parent, recording.generation, call_slots=call_slots)


def test_resumed_generation_restores_reservation_order_without_occupying_capture_slots(bundle: Path) -> None:
    slots = threading.BoundedSemaphore(1)
    resumed = resumed_generation(bundle, call_slots=slots)
    with slots, ThreadPoolExecutor(max_workers=2) as pool:
        second = pool.submit(resumed.create, stream=True, model="fixture", input="image", request_id=1)
        with pytest.raises(FutureTimeout):
            second.result(timeout=0.1)
        first = pool.submit(resumed.create, stream=True, model="fixture", input="image")
        assert second.result(timeout=5).output_text == "second"
        assert first.result(timeout=5).output_text == "first"


def test_resumed_generation_honors_saved_delivery_order(bundle: Path) -> None:
    resumed = resumed_generation(bundle)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resumed.create, stream=True, model="fixture", input="image")
        with pytest.raises(FutureTimeout):
            first.result(timeout=0.1)
        second = pool.submit(resumed.create, stream=True, model="fixture", input="image", request_id=1)
        assert second.result(timeout=5).output_text == "second"
        assert first.result(timeout=5).output_text == "first"


def test_resumed_generation_fails_if_preceding_delivery_is_never_consumed(bundle: Path) -> None:
    resumed = resumed_generation(bundle, timeout=0.05)
    with pytest.raises(WitnessError, match="delivery order"):
        resumed.create(stream=True, model="fixture", input="image")
    with pytest.raises(WitnessError, match="unresolved"):
        resumed.finish("source")
