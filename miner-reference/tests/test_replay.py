"""Run the miner against an offline recording with the real replay client."""

import hashlib
import threading
import uuid
from pathlib import Path
from typing import Unpack

import pytest
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming
from renderer_client import RecordingRenderer, ReplayRenderer
from test_batch import PNG, Calls, Renderer
from witness_client import ReplayWitness, WitnessResult
from witness_client.calls import Generation, RequestKey, canonical
from witness_client.pods import CallView, EntryView, RecordingView
from witness_client.request import serialize_body

from miner_reference.pipeline import normalize, run


@pytest.mark.parametrize("repair", [False, True])
def test_complete_five_candidate_pipeline_reconstructs_from_recordings(tmp_path: Path, repair: bool) -> None:
    generation = Generation.from_prompt(PNG, 42, "a" * 64, round_number="test-0")
    entries: list[EntryView] = []
    calls: dict[str, CallView] = {}
    lock = threading.Lock()
    delivered = {i: threading.Event() for i in (1, 2, 3, 5, 6)}

    class Capture(Calls):
        def create(self, *, witness_seed: int = 0, **params: Unpack[ResponseCreateParamsStreaming]) -> WitnessResult:
            value = super().create(witness_seed=witness_seed, **params)
            body = serialize_body(params, "api")
            key = RequestKey.from_body("api", body, witness_seed)
            with lock:
                ordinal = len(entries)
                entries.append(EntryView(call_id=key.id, delivery=None))
            if witness_seed in (1, 2, 5):
                assert delivered[witness_seed + 1].wait(timeout=5)
            exchange_id = str(uuid.uuid4())
            directory = tmp_path / exchange_id
            directory.mkdir(mode=0o700)
            response = (
                b"data: " + canonical({"type": "response.completed", "response": value.response.model_dump()}) + b"\n\n"
            )
            manifest = canonical(
                {
                    "generation_id": generation.id,
                    "call_id": key.id,
                    "provider_status": 200,
                    "response_sha256": hashlib.sha256(response).hexdigest(),
                }
            )
            (directory / "response.sse").write_bytes(response)
            (directory / "manifest.json").write_bytes(manifest)
            with lock:
                delivery = sum(entry.delivery is not None for entry in entries)
                entries[ordinal] = EntryView(call_id=key.id, delivery=delivery)
                calls[key.id] = CallView(
                    request=key,
                    exchange_id=exchange_id,
                    owner_ordinal=ordinal,
                    deadline=2000000000,
                    phase="issued",
                    authorized=True,
                    manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                )
            if witness_seed in delivered:
                delivered[witness_seed].set()
            return value

    live = Capture()
    underlying = Renderer()
    if repair:
        underlying.reject = {
            normalize(Calls().create(witness_seed=i, stream=True, model="gpt-6.1-sol").output_text)
            for i in (1, 2, 3, 5, 6)
        }
    renderer = RecordingRenderer(underlying)
    expected = run(PNG, 42, live, renderer)
    assert expected is not None and len(live.requests) == (12 if repair else 7)
    recording = (
        RecordingView(
            version=1,
            revision=str(uuid.uuid4()),
            scope_key="fixture",
            generation=generation,
            entries=entries,
            calls=calls,
            output_sha256=hashlib.sha256(expected.encode()).hexdigest(),
            output_is_none=False,
            finished=True,
        )
        .model_dump_json()
        .encode()
    )
    (tmp_path / "recording.json").write_bytes(recording)
    (tmp_path / "verified.json").write_bytes(
        canonical(
            {
                "version": 1,
                "recording_sha256": hashlib.sha256(recording).hexdigest(),
                "hotkey": "hotkey",
                "provider": "api",
            }
        )
    )
    for _ in range(2):
        replay = ReplayWitness(tmp_path)
        previews = ReplayRenderer(renderer.records, renderer_id=renderer.renderer_id)
        actual = run(PNG, 42, replay.responses, previews)
        assert actual == expected
        replay.finish(actual)
        previews.assert_consumed()
    assert len(live.requests) == (12 if repair else 7)
