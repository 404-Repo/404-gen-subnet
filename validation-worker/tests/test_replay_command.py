"""Manual replay outcomes and logs; provider and renderer I/O are replaced at their boundaries."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import openai
import pytest
from openai.types.responses.easy_input_message_param import EasyInputMessageParam
from openai.types.responses.response_input_message_content_list_param import ResponseInputMessageContentListParam
from openai.types.responses.response_input_param import ResponseInputParam
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from replay_fixtures import PRICING, SCOPE, Capture
from subnet_common.witness.pricing import BudgetRejected
from test_manual_replay import Stored
from witness_client import AsyncWitnessClient, WitnessError
from witness_client.calls import RequestKey, canonical

from validation_worker import manual_replay as local_replay
from validation_worker.manual_replay import Log, parser, run_prompts
from validation_worker.replay_evidence import (
    SavedReplaySource,
    export_requests,
)
from validation_worker.replay_worker import Witness, invoke_generate
from validation_worker.request_replay import (
    EvidenceUnavailable,
    PreparedReplay,
    RecordedWitnessClient,
    ReplayMismatch,
    ReplaySource,
    RequestInventory,
    SavedCapture,
    png_request,
    png_request_tolerance,
)


def replay_requests(
    tmp_path: Path,
    provider: Literal["api", "codex"] = "api",
    *,
    other_input: str | ResponseInputParam | None = None,
    model_input: str | ResponseInputParam = "image",
    image: bytes = b"PNG",
    failure: Literal["http", "provider", "capture", "bad_request"] | None = None,
    request_count: int = 2,
) -> RecordedWitnessClient:
    live = Capture(tmp_path)
    live._mode = provider
    for request_id in range(request_count):
        key, body = live._request(request_id, None, {"model": "fixture", "input": model_input})
        if request_id == 0 and failure is not None:
            if failure == "provider":
                live.outcome = (
                    200,
                    b"data: "
                    + canonical(
                        {
                            "type": "error",
                            "code": "server_is_overloaded",
                            "message": "Server overloaded",
                            "param": None,
                        }
                    )
                    + b"\n\n",
                    None,
                )
            elif failure == "bad_request":
                live.outcome = (
                    400,
                    canonical(
                        {
                            "error": {
                                "message": "Unsupported parameter",
                                "code": "unsupported_parameter",
                                "param": "temperature",
                            }
                        }
                    ),
                    {"input_tokens": 10, "output_tokens": 2},
                )
            else:
                live.failures[0] = "provider" if failure == "http" else "unknown"
        try:
            live._capture(key, body, None)
        except WitnessError:
            assert request_id == 0 and failure in ("provider", "capture")
        live.outcome = None
        if request_id == 0 and failure in ("provider", "capture"):
            record = live.records.load(SCOPE, key.id)
            assert record is not None
            if failure == "provider":
                # Historical journals can mark an HTTP 200 provider error as issued.
                record.call.phase, record.call.failure = "issued", None
                live.state.put(live.records.key(SCOPE, key.id), record.model_dump_json().encode())
            else:
                prefix = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{record.call.exchange_id}/"
                live.state.put(prefix + "request.json", body)
                live.state.put(
                    prefix + "status.json", canonical({"status": "failed", "error_type": record.call.failure})
                )
    if other_input is not None:
        live._mode = "codex" if provider == "api" else "api"
        key, body = live._request(0, None, {"model": "fixture", "input": other_input})
        live._capture(key, body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    directory = tmp_path / "bundle"
    inventory = export_requests(objects, SCOPE, directory, PRICING, lambda *a, **k: None)

    source = SavedReplaySource(
        objects,
        objects,
        SCOPE,
        directory,
        inventory,
        image_cache=tmp_path / "image-cache",
        emit=lambda *a, **k: None,
    )
    return RecordedWitnessClient(inventory, PRICING, source, lambda *a, **k: None, reference=image)


@pytest.mark.parametrize("operation", ["next", "close"])
def test_async_recorded_stream_allows_miner_tasks_during_blocked_logging(tmp_path: Path, operation: str) -> None:
    import asyncio

    witness = replay_requests(tmp_path)
    entered = threading.Event()
    released = threading.Event()

    def emit(event: str, **fields: object) -> None:
        if event == "model_operation" and fields.get("action") == operation:
            entered.set()
            if not released.wait(2):
                raise TimeoutError("Stream logging blocked the miner event loop")

    witness.emit = emit

    async def generate(image: bytes, seed: int, client: AsyncWitnessClient) -> str:
        stream = await client.responses.create(model="fixture", input="image", stream=True)

        async def release_log() -> None:
            assert await asyncio.to_thread(entered.wait, 2)
            released.set()

        release = asyncio.create_task(release_log())
        try:
            if operation == "next":
                assert (await stream.__anext__()).type == "response.completed"
            else:
                await stream.aclose()
            await release
        finally:
            released.set()
            await stream.aclose()
            await release
        return "completed"

    assert invoke_generate(generate, b"PNG", 42, witness, None) == "completed"
    witness.finish()


def test_sync_replay_matches_requests_reuses_response_and_counts_once(tmp_path: Path) -> None:
    witness = replay_requests(tmp_path)

    def generate(image: bytes, seed: int, client: RecordedWitnessClient | Witness, renderer: None) -> str:
        a = client.responses.create(model="fixture", input="image").output_text
        b = client.responses.create(model="fixture", input="image").output_text
        return a + b

    assert invoke_generate(generate, b"PNG", 42, witness, None) == "00"
    witness.finish()
    assert len(witness.budget.charges) == 1
    assert len(witness.used) == 1
    assert len(witness.inventory.captures) == 2


def test_manual_replay_uses_selected_pricing(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    rates = json.loads(PRICING)
    rates["models"]["fixture"].update(input_credits_per_token=0.2, output_credits_per_token=1)
    pricing = canonical(rates)
    directory = tmp_path / "bundle"
    inventory = export_requests(objects, SCOPE, directory, pricing, lambda *a, **k: None)
    source = SavedReplaySource(
        objects,
        objects,
        SCOPE,
        directory,
        inventory,
        image_cache=tmp_path / "images",
        emit=lambda *a, **k: None,
    )
    for snapshot in (inventory, inventory.model_copy(update={"pricing_sha256": hashlib.sha256(PRICING).hexdigest()})):
        witness = RecordedWitnessClient(snapshot, pricing, source, lambda *a, **k: None)
        assert witness.create(model="fixture", input="image").output_text == "0"
        witness.finish()
        witness.budget.accept()
        assert witness.budget.charges == {key.id: Decimal("4")}


@pytest.mark.parametrize("provider", ["api", "codex"])
def test_async_replay_uses_current_three_argument_interface_with_parallel_calls(
    tmp_path: Path, provider: Literal["api", "codex"]
) -> None:
    witness = replay_requests(tmp_path, provider, other_input="unrelated prompt")

    async def generate(image: bytes, seed: int, client: AsyncWitnessClient) -> str:
        import asyncio

        values = await asyncio.gather(
            *(client.responses.create(request_id=i, model="fixture", input="image") for i in (1, 0))
        )
        return "".join(value.output_text for value in values)

    assert invoke_generate(generate, b"PNG", 42, witness, None) == "10"
    witness.finish()
    assert len(witness.budget.charges) == 2


def test_ambiguous_saved_requests_stop_replay(tmp_path: Path) -> None:
    witness = replay_requests(tmp_path, other_input="image")
    with pytest.raises(ReplayMismatch, match="More than one saved provider"):
        witness.responses.create(model="fixture", input="image")
    with pytest.raises(ReplayMismatch):
        witness.finish()


@pytest.mark.parametrize("provider", ["api", "codex"])
def test_uncaptured_request_allows_connection_error_fallback(tmp_path: Path, provider: Literal["api", "codex"]) -> None:
    witness = replay_requests(tmp_path, provider)
    with pytest.raises(openai.APIConnectionError):
        witness.responses.create(model="fixture", input="changed")
    assert witness.responses.create(request_id=1, model="fixture", input="image").output_text == "1"
    witness.finish()
    witness.budget.accept()
    assert len(witness.budget.charges) == 1
    assert len(witness.assumed_failures) == 1


def test_caught_local_validation_error_allows_fallback(tmp_path: Path) -> None:
    witness = replay_requests(tmp_path)
    with pytest.raises(ValueError, match="background"):
        witness.responses.create(model="fixture", input="image", background=True)
    assert witness.responses.create(model="fixture", input="image").output_text == "0"
    witness.finish()
    witness.budget.accept()


def test_missing_usage_is_excluded_but_known_usage_still_enforces_call_budget(tmp_path: Path) -> None:
    witness = replay_requests(tmp_path, failure="provider")
    witness.budget.rates["calls_per_prompt"] = 1
    with pytest.raises(openai.APIError):
        witness.responses.create(model="fixture", input="image")
    assert witness.responses.create(request_id=1, model="fixture", input="image").output_text == "1"
    witness.finish()
    witness.budget.accept()
    assert len(witness.unknown_usage) == 1 and len(witness.budget.charges) == 1
    witness.budget.rates["calls_per_prompt"] = 0
    with pytest.raises(BudgetRejected, match="exceeds"):
        witness.budget.accept()


def test_unreadable_capture_cannot_become_a_connection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    witness = replay_requests(tmp_path)

    def missing(exchange_id: str) -> tuple[SavedCapture, bytes]:
        raise FileNotFoundError("Saved response is missing")

    monkeypatch.setattr(witness.source, "fetch", missing)
    with pytest.raises(FileNotFoundError, match="Saved response"):
        witness.responses.create(model="fixture", input="image")
    with pytest.raises(FileNotFoundError):
        witness.finish()
    assert not witness.budget.charges


def test_cached_index_miss_loads_existing_response_instead_of_assuming_failure(tmp_path: Path) -> None:
    witness = replay_requests(tmp_path)
    witness.inventory.captures.clear()
    witness.by_key.clear()
    assert witness.responses.create(model="fixture", input="image").output_text == "0"
    witness.finish()
    assert not witness.assumed_failures and len(witness.used) == 1


def test_journal_lookup_error_cannot_be_caught_as_an_assumed_connection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = replay_requests(tmp_path)

    def unavailable(request: object) -> dict[str, SavedCapture]:
        raise EvidenceUnavailable("R2 unavailable")

    monkeypatch.setattr(witness.source, "lookup", unavailable)
    with pytest.raises(EvidenceUnavailable, match="R2 unavailable"):
        witness.responses.create(model="fixture", input="uncaptured")
    with pytest.raises(EvidenceUnavailable):
        witness.finish()
    assert not witness.assumed_failures


def test_recorded_bad_request_replays_sdk_details_and_charges_known_usage(tmp_path: Path) -> None:
    witness = replay_requests(tmp_path, failure="bad_request")
    with pytest.raises(openai.BadRequestError) as caught:
        witness.responses.create(model="fixture", input="image")
    assert caught.value.status_code == 400 and caught.value.code == "unsupported_parameter"
    assert caught.value.param == "temperature"
    assert witness.responses.create(request_id=1, model="fixture", input="image").output_text == "1"
    witness.finish()
    witness.budget.accept()
    assert len(witness.budget.charges) == 2 and not witness.unknown_usage


def png_bytes(change: str = "compression", compression: int = 6, *, size: tuple[int, int] = (4, 4)) -> bytes:
    image = Image.new("RGBA", (size[0] + 1, size[1]) if change == "size" else size, (10, 20, 30, 255))
    if change in ("pixel", "two_pixels", "large_delta"):
        image.putpixel((1, 1), (12 if change == "large_delta" else 11, 20, 30, 255))
        if change == "two_pixels":
            image.putpixel((2, 1), (11, 20, 30, 255))
    elif change == "alpha":
        image.putpixel((1, 1), (10, 20, 30, 254))
    metadata = PngInfo()
    metadata.add_text("Description", "changed" if change == "metadata" else "original")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=compression, pnginfo=metadata)
    return buffer.getvalue()


def png_input(
    change: str = "compression", compression: int = 6, *, size: tuple[int, int] = (4, 4)
) -> ResponseInputParam:
    url = "data:image/png;base64," + base64.b64encode(png_bytes(change, compression, size=size)).decode("ascii")
    content: ResponseInputMessageContentListParam = [{"type": "input_image", "image_url": url, "detail": "auto"}]
    if change == "text":
        content.append({"type": "input_text", "text": "different"})
    message: EasyInputMessageParam = {"role": "user", "content": content}
    return [message]


@pytest.mark.parametrize("change", ["compression", "pixel", "alpha", "size", "metadata"])
def test_png_matching_preserves_pixels_dimensions_transparency_and_metadata(change: str) -> None:
    original = json.dumps({"input": png_input(compression=0)}).encode()
    actual = json.dumps({"input": png_input(change, compression=9)}).encode()
    assert original != actual
    assert (png_request(original) == png_request(actual)) == (change == "compression")


@pytest.mark.parametrize(
    "change", ["pixel", "two_pixels", "large_delta", "alpha", "size", "metadata", "text", "model", "reference"]
)
def test_image_tolerance_limits_changed_pixels_and_preserves_other_inputs(change: str) -> None:
    size = (200, 100)
    original = canonical({"model": "fixture", "input": png_input(compression=0, size=size)})
    actual = canonical(
        {
            "model": "different" if change == "model" else "fixture",
            "input": png_input("pixel" if change == "reference" else change, compression=9, size=size),
        }
    )
    reference = png_bytes(size=size) if change == "reference" else None
    matched = png_request_tolerance(actual, original, reference)
    if change != "pixel":
        assert matched is None
        return
    assert matched is not None and len(matched) == 1
    assert matched[0].changed_pixels == 1 and matched[0].total_pixels == 20_000
    assert matched[0].max_channel_delta == 1


def test_image_tolerance_checks_each_image_separately() -> None:
    size = (200, 100)
    original = canonical({"input": png_input(size=size) + png_input(size=size)})
    actual = canonical({"input": png_input("pixel", size=size) + png_input("two_pixels", size=size)})
    assert png_request_tolerance(actual, original, None) is None


def test_image_tolerance_preserves_rgb_transparency() -> None:
    requests = []
    for color in ((10, 20, 30), (11, 20, 30)):
        image = Image.new("RGB", (200, 100), (10, 20, 30))
        image.putpixel((1, 1), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", transparency=(10, 20, 30))
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        requests.append(canonical({"input": [{"type": "input_image", "image_url": url}]}))
    assert png_request_tolerance(requests[0], requests[1], None) is None


@pytest.mark.parametrize("provider", ["api", "codex"])
def test_exact_png_match_wins_over_tolerance_on_the_other_provider(
    tmp_path: Path, provider: Literal["api", "codex"]
) -> None:
    size = (200, 100)
    witness = replay_requests(
        tmp_path,
        provider,
        model_input=png_input(compression=0, size=size),
        other_input=png_input("pixel", size=size),
        image=png_bytes(),
    )
    assert witness.responses.create(model="fixture", input=png_input(compression=9, size=size)).output_text == "0"
    witness.finish()
    assert witness._mode == provider
    assert not witness.image_tolerance_matches
    assert list(witness.request_matches.values()) == ["exact_png_pixels"]


@pytest.mark.parametrize("provider", ["api", "codex"])
@pytest.mark.parametrize(
    "change", ["compression", "pixel", "model", "text", "request_id", "ambiguous", "tolerance", "tolerance_ambiguous"]
)
def test_replay_matches_png_encoding_and_keeps_other_request_fields_exact(
    tmp_path: Path, provider: Literal["api", "codex"], change: str
) -> None:
    live = Capture(tmp_path)
    live._mode = provider
    size = (200, 100) if change.startswith("tolerance") else (4, 4)
    original = png_input(compression=0, size=size)
    key, body = live._request(0, None, {"model": "fixture", "input": original})
    live._capture(key, body, None)
    if change in ("ambiguous", "tolerance_ambiguous"):
        other_key, other_body = live._request(
            0, None, {"model": "fixture", "input": png_input(compression=3, size=size)}
        )
        live._capture(other_key, other_body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    directory = tmp_path / "bundle"
    inventory = export_requests(objects, SCOPE, directory, PRICING, lambda *a, **k: None)
    events: list[str] = []

    source = SavedReplaySource(
        objects,
        objects,
        SCOPE,
        directory,
        inventory,
        image_cache=tmp_path / "image-cache",
        emit=lambda *a, **k: None,
    )
    witness = RecordedWitnessClient(
        inventory, PRICING, source, lambda event, **fields: events.append(event), reference=None
    )
    actual = png_input("pixel" if change.startswith("tolerance") else change, compression=9, size=size)
    model = "changed" if change == "model" else "fixture"
    request_id = 1 if change == "request_id" else 0
    if change not in ("compression", "tolerance"):
        error_type = ReplayMismatch if change in ("ambiguous", "tolerance_ambiguous") else openai.APIConnectionError
        with pytest.raises(error_type):
            witness.responses.create(model=model, input=actual, request_id=request_id)
        if change in ("ambiguous", "tolerance_ambiguous"):
            with pytest.raises(ReplayMismatch):
                witness.finish()
        else:
            witness.finish()
        assert not witness.budget.charges
        return
    assert witness.responses.create(model=model, input=actual).output_text == "0"
    assert witness.responses.create(model=model, input=original).output_text == "0"
    witness.finish()
    assert list(witness.budget.charges) == [key.id]
    assert len(witness.used) == 1
    assert ("image_tolerance_matched" if change == "tolerance" else "png_request_matched") in events
    assert list(witness.request_matches.values()).count("exact") == 1
    assert (
        list(witness.request_matches.values()).count("image_tolerance" if change == "tolerance" else "exact_png_pixels")
        == 1
    )


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["response", "stream"])
def test_journal_lookup_updates_png_matches_in_worker_and_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
    stream: bool,
) -> None:
    reference = png_bytes(compression=0)
    live = Capture(tmp_path)
    original, _ = live._request(0, None, {"model": "fixture", "input": png_input(compression=0)})
    for model_input in (png_input(compression=0), png_input("pixel")):
        key, body = live._request(0, None, {"model": "fixture", "input": model_input})
        live._capture(key, body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    inventory = export_requests(objects, SCOPE, tmp_path / "recordings", PRICING, lambda *a, **k: None)
    inventory.captures = {
        exchange: item for exchange, item in inventory.captures.items() if item.call.request != original
    }
    pricing = tmp_path / "pricing.json"
    pricing.write_bytes(PRICING)

    async def prepare(*, output: Path, **fields: object) -> PreparedReplay:
        bundle = output / "bundle"
        bundle.mkdir()
        (bundle / "requests.json").write_text(inventory.model_dump_json())
        (bundle / "pricing.json").write_bytes(PRICING)
        prompt = output / "prompts/one"
        prompt.mkdir(parents=True)
        (prompt / "expected.json").write_text(json.dumps({"available": True, "output": "0"}))
        return PreparedReplay.model_validate(
            {
                "competition_repository": "fixture/competition",
                "competition_ref": "a" * 40,
                "round": SCOPE.round,
                "hotkey": SCOPE.hotkey,
                "source_repository": "fixture/miner",
                "source_commit": "b" * 40,
                "storage_prefix": "",
                "seed": 42,
                "prompts": [{"id": "one", "sha256": hashlib.sha256(reference).hexdigest()}],
                "providers": ["api"],
            }
        )

    def execute(
        *,
        directory: Path,
        plan: dict[str, Any],
        source: ReplaySource,
        log: Log,
        **fields: object,
    ) -> dict[str, Any]:
        Path(plan["output"]).mkdir(parents=True)

        witness = RecordedWitnessClient(
            RequestInventory.model_validate_json((directory / "bundle/requests.json").read_bytes()),
            (directory / "bundle/pricing.json").read_bytes(),
            source,
            log.emit,
            reference=reference,
        )
        assert witness.create(model="fixture", input=png_input(compression=0)).output_text == "0"
        if asynchronous:

            async def generate(image: bytes, seed: int, client: AsyncWitnessClient) -> str:
                if stream:
                    async with await client.create(
                        model="fixture", input=png_input(compression=9), stream=True
                    ) as events:
                        event = await events.__anext__()
                        assert event.type == "response.completed"
                        return event.response.output_text
                return (await client.create(model="fixture", input=png_input(compression=9))).output_text

            assert invoke_generate(generate, reference, 42, witness, None) == "0"
        elif stream:
            with witness.create(model="fixture", input=png_input(compression=9), stream=True) as events:
                event = next(events)
                assert event.type == "response.completed" and event.response.output_text == "0"
        else:
            assert witness.create(model="fixture", input=png_input(compression=9)).output_text == "0"
        witness.finish()
        assert not witness.assumed_failures and len(witness.budget.charges) == 1
        return {"prompt": "one", "passed": True}

    monkeypatch.setattr("boto3.client", lambda *a, **k: None)
    monkeypatch.setattr("validation_worker.replay_evidence.R2Evidence", lambda *a, **k: objects)
    monkeypatch.setattr("validation_worker.replay_evidence.prepare", prepare)
    monkeypatch.setattr(local_replay, "execute_prompt", execute)
    monkeypatch.setattr(local_replay, "INDEX_CACHE", tmp_path / "indexes")
    for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_REPLAY_BUCKET"):
        monkeypatch.setenv(name, "fixture-value")
    monkeypatch.setattr(
        "sys.argv",
        [
            "replay.py",
            "--hotkey",
            SCOPE.hotkey,
            "--round",
            SCOPE.round,
            "--all",
            "--deployment",
            SCOPE.deployment,
            "--witness-image",
            "sha256:" + "f" * 64,
            "--pricing",
            str(pricing),
            "--output",
            str(tmp_path / "replay"),
        ],
    )
    assert local_replay.main() == 0

    events = [json.loads(line) for line in (tmp_path / "replay/events.jsonl").read_text().splitlines()]
    request = [event for event in events if event["event"] == "model_request"][-1]
    actual = RequestKey.from_body("api", request["body"].encode())
    assert request["request_key"] == actual.model_dump()
    assert request["call_id"] == actual.id
    assert request["recorded_request_key"] == original.model_dump()
    assert actual != original
    for event_name in ("provider_selected", "model_response"):
        assert {event["call_id"] for event in events if event["event"] == event_name} == {original.id, actual.id}
    operations = [event for event in events if event["event"] == "model_operation"]
    assert {event["call_id"] for event in operations if event["action"] == "create"} == {original.id, actual.id}
    actual_operations = [event for event in operations if event["call_id"] == actual.id]
    assert all(event["occurrence"] == 0 for event in actual_operations)
    assert {event["action"] for event in actual_operations} == ({"create", "next", "close"} if stream else {"create"})


def test_reencoded_request_reproduces_capture_error_and_allows_fallback(tmp_path: Path) -> None:
    original = png_input(compression=0)
    reference = png_bytes(compression=0)
    logged: list[dict[str, Any]] = []
    witness = replay_requests(tmp_path, model_input=original, image=reference, failure="capture")
    witness.emit = lambda event, **fields: logged.append({"event": event, **fields})
    with pytest.raises(openai.APIConnectionError):
        witness.responses.create(model="fixture", input=png_input(compression=9))
    assert witness.responses.create(request_id=1, model="fixture", input=png_input(compression=9)).output_text == "1"
    witness.finish()
    witness.budget.accept()
    assert len(witness.used) == 2 and len(witness.unknown_usage) == len(witness.budget.charges) == 1
    assert witness.problem is None
    assert any(event["event"] == "recorded_capture_error" for event in logged)
    assert any(
        event["event"] == "model_request" and json.loads(event["body"])["input"] == png_input(compression=9)
        for event in logged
    )


@pytest.mark.parametrize("mode,seen", [("stop", ["one"]), ("continue", ["one", "two"])])
def test_batch_stops_by_default_or_continues_to_next_prompt_and_keeps_failure(
    tmp_path: Path,
    mode: str,
    seen: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    log = Log(tmp_path)

    def execute(prompt: dict[str, Any]) -> dict[str, Any]:
        calls.append(prompt["id"])
        return {"prompt": prompt["id"], "passed": prompt["id"] != "one"}

    try:
        result = run_prompts({"prompts": [{"id": "one"}, {"id": "two"}]}, execute, mode, log)
    finally:
        log.close()
    assert calls == seen
    assert not result["passed"]
    assert json.loads((tmp_path / "summary.json").read_bytes())["results"] == result["results"]
    assert "replay_finished" in capsys.readouterr().out


def test_batch_reports_exact_tolerant_and_recovered_passes_separately(tmp_path: Path) -> None:
    log = Log(tmp_path)
    results = {
        "exact": {"passed": True, "image_tolerance_matches": []},
        "tolerant": {"passed": True, "image_tolerance_matches": [{"call_id": "one"}]},
        "recovered": {"passed": True, "recovered": True, "image_tolerance_matches": [{"call_id": "three"}]},
        "failed": {"passed": False, "image_tolerance_matches": [{"call_id": "two"}]},
    }
    try:
        summary = run_prompts(
            {"prompts": [{"id": key} for key in results]}, lambda prompt: results[prompt["id"]], "continue", log
        )
    finally:
        log.close()
    assert summary["exact_passed_prompts"] == summary["tolerant_passed_prompts"] == 1
    assert summary["recovered_passed_prompts"] == 1
    assert summary["completed_prompts"] == 4 and not summary["passed"]


def test_parallel_batch_is_bounded_and_returns_results_in_prompt_order(tmp_path: Path) -> None:
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    maximum = 0
    log = Log(tmp_path)

    def execute(prompt: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return {"prompt": prompt["id"], "passed": True}

    try:
        result = run_prompts({"prompts": [{"id": str(i)} for i in range(6)]}, execute, "continue", log, jobs=3)
    finally:
        log.close()
    assert result["passed"] and result["completed_prompts"] == 6
    assert maximum == 3
    assert [r["prompt"] for r in result["results"]] == [str(i) for i in range(6)]


@pytest.mark.parametrize("mode,expected", [("stop", ["one", "two"]), ("continue", ["one", "two", "three"])])
def test_parallel_failure_stops_new_work_and_finishes_started_prompts(
    tmp_path: Path, mode: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = threading.Barrier(2)
    release = threading.Event()
    calls: list[str] = []
    log = Log(tmp_path)
    original = log.emit

    def emit(event: str, **fields: object) -> None:
        original(event, **fields)
        if event == "replay_stopping":
            release.set()

    monkeypatch.setattr(log, "emit", emit)

    def execute(prompt: dict[str, Any]) -> dict[str, Any]:
        calls.append(prompt["id"])
        if prompt["id"] in ("one", "two"):
            barrier.wait(timeout=5)
        if prompt["id"] == "two":
            assert release.wait(timeout=5)
        if prompt["id"] == "three":
            release.set()
        return {"prompt": prompt["id"], "passed": prompt["id"] != "one"}

    try:
        result = run_prompts({"prompts": [{"id": x} for x in ("one", "two", "three")]}, execute, mode, log, jobs=2)
    finally:
        release.set()
        log.close()
    assert sorted(calls) == sorted(expected)
    assert [r["prompt"] for r in result["results"]] == expected
    assert not result["passed"]
    assert result["stopped_on_mismatch"] == (mode == "stop")
    assert json.loads((tmp_path / "summary.json").read_bytes())["completed_prompts"] == len(expected)


def test_parallel_interrupt_cancels_workers_and_saves_completed_results(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    cancelled = threading.Event()
    calls: list[str] = []
    log = Log(tmp_path)

    def execute(prompt: dict[str, Any]) -> dict[str, Any]:
        calls.append(prompt["id"])
        barrier.wait(timeout=5)
        if prompt["id"] == "one":
            raise KeyboardInterrupt
        assert cancelled.wait(timeout=5)
        return {"prompt": prompt["id"], "passed": False}

    try:
        with pytest.raises(KeyboardInterrupt):
            run_prompts(
                {"prompts": [{"id": x} for x in ("one", "two", "three")]},
                execute,
                "continue",
                log,
                jobs=2,
                cancel=cancelled.set,
            )
    finally:
        cancelled.set()
        log.close()
    summary = json.loads((tmp_path / "summary.json").read_bytes())
    assert sorted(calls) == ["one", "two"]
    assert cancelled.is_set() and summary["interrupted"] and not summary["passed"]
    assert summary["started_prompts"] == 2 and summary["not_started_prompts"] == 1
    assert summary["results"] == [{"prompt": "two", "passed": False}]


def test_console_summarizes_payloads_and_saved_logs_keep_full_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = 'data: {"type":"response.output_text.delta","delta":"token"}\n\n' * 5000
    image = "data:image/png;base64," + "a" * 500_000
    source = "function generatedSource() {}" * 10000
    log = Log(tmp_path, ["private-render-key"])
    try:
        log.emit(
            "model_request", request_key={"provider": "codex", "request_id": 7}, body=json.dumps({"image_url": image})
        )
        log.emit("model_response", provider_status=200, body=body, usage={"input_tokens": 10, "output_tokens": 2})
        log.emit("render_request", index=3, source=source)
        log.emit("output_comparison", exact=True, expected=source, reproduced=source)
        log.emit("miner_log", prompt="one", text="native stdout and stderr\n")
        log.emit("miner_log", prompt="one", text="preview " + image)
        log.emit("miner_log", prompt="one", text='data: {"type":"response.output_text.delta","delta":"token"}\n')
        try:
            raise RuntimeError("Renderer returned HTTP 502 private-render-key")
        except RuntimeError:
            log.exception("renderer_unavailable", prompt="one")
    finally:
        log.close()
    console = capsys.readouterr().out
    saved = (tmp_path / "console.log").read_text()
    assert len(console) < 5000
    assert "model_request" in console and "model_response" in console and "render_request" in console
    assert '"request_id":7' in console.replace(" ", "") and "codex" in console
    assert "502" in console and "output_comparison" in console
    assert "response.output_text.delta" not in console and "base64," not in console
    assert "generatedSource" not in console
    assert body in saved and image in saved and source in saved
    assert "native stdout and stderr" in saved
    structured = (tmp_path / "events.jsonl").read_text()
    assert "private-render-key" not in console + saved + structured
    assert any(json.loads(line).get("body") == body for line in structured.splitlines())


def test_console_reports_index_and_batch_progress_without_dumping_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = Log(tmp_path)
    try:
        for i in range(300):
            log.emit("capture_indexed", exchange_id=f"capture-{i}", phase="issued")
        log.emit("model_operation", action="next", index=123)
        log.emit(
            "replay_finished",
            passed=False,
            selected_prompts=128,
            completed_prompts=2,
            results=[{"passed": True, "unused_exchanges": ["unused-id"] * 1000}, {"passed": False, "error": "test"}],
        )
    finally:
        log.close()
    console = capsys.readouterr().out
    assert len(console.splitlines()) < 10
    assert "300" in console and "replay_finished" in console
    assert "unused-id" not in console and "model_operation" not in console
    saved = (tmp_path / "console.log").read_text()
    assert "capture-299" in saved and "unused-id" in saved and "model_operation" in saved


def test_miner_js_stays_in_saved_logs_while_replay_progress_and_errors_are_visible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    miner_output = [
        "export default function generatedScene(THREE) {\n",
        "  const scene = new THREE.Group();\n",
        '  console.error("shader error");\n',
        "  return scene;\n",
        "}\n",
        'const inlineScene = () => "generated JS";\n',
    ]
    log = Log(tmp_path)
    try:
        log.emit("prompt_started", prompt="one", index=1, total=128)
        for line in miner_output:
            log.emit("miner_log", prompt="one", text=line)
        log.emit("model_response", provider_status=200, usage={"input_tokens": 10, "output_tokens": 2})
        log.emit("render_response", index=0, bytes=1000)
        log.emit("replay_error", error_type="RenderUnavailable", error="Renderer returned HTTP 502")
    finally:
        log.close()
    console = capsys.readouterr().out
    assert "prompt_started" in console and "model_response" in console and "render_response" in console
    assert "RenderUnavailable" in console and "HTTP 502" in console
    assert all(
        token not in console
        for token in ("generatedScene", "THREE.Group", "shader error", "return scene", "inlineScene")
    )
    saved = (tmp_path / "console.log").read_text()
    assert all(line in saved for line in miner_output)
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [row["text"] for row in rows if row["event"] == "miner_log"] == miner_output


@pytest.mark.parametrize("key_name", ["REPLAY_RENDERER_ID", "WITNESS_IMAGE"])
def test_cli_uses_original_witness_image_and_defaults_to_stop(monkeypatch: pytest.MonkeyPatch, key_name: str) -> None:
    monkeypatch.delenv("REPLAY_RENDERER_ID", raising=False)
    monkeypatch.delenv("WITNESS_IMAGE", raising=False)
    monkeypatch.setenv(key_name, "witness@sha256:" + "a" * 64)
    parsed = parser().parse_args(["--hotkey", SCOPE.hotkey, "--round", "test-0", "--all"])
    assert parsed.on_mismatch == "stop"
    assert parsed.jobs == 1
    assert parsed.renderer_id == "witness@sha256:" + "a" * 64


@pytest.mark.parametrize("jobs", ["0", "-1"])
def test_cli_rejects_nonpositive_concurrency_before_preparing_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: str
) -> None:
    output = tmp_path / "replay"
    monkeypatch.setattr(
        "sys.argv",
        [
            "replay.py",
            "--hotkey",
            SCOPE.hotkey,
            "--round",
            "test-0",
            "--all",
            "--renderer-id",
            "fixture",
            "--deployment",
            SCOPE.deployment,
            "--jobs",
            jobs,
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit, match="--jobs must be at least 1"):
        local_replay.main()
    assert not output.exists()


def test_script_runs_from_another_directory_with_isolated_imports(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "replay.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "--hotkey" in result.stdout and "--witness-image" in result.stdout
