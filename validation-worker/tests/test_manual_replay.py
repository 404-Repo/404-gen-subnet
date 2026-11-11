"""Read-only source preparation and saved response loading use offline storage fixtures."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import TypedDict

import httpx
import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.response import StreamingBody
from openai.types.responses.easy_input_message_param import EasyInputMessageParam
from PIL import Image
from renderer_client import RenderOptions, RenderRejected, RenderUnavailable
from replay_fixtures import PRICING, SCOPE, Capture
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.github import GitHubClient
from subnet_common.witness.models import Scope, json_bytes
from subnet_common.witness.rounds import PromptImage, RoundInputs
from subnet_common.witness.state import R2StateStore
from urllib3 import HTTPConnectionPool
from urllib3.exceptions import ReadTimeoutError as SocketReadTimeout
from witness_client import WitnessError
from witness_client.calls import canonical
from witness_pod.records import Call

from validation_worker.replay_evidence import (
    R2Evidence,
    SavedRenderer,
    export_requests,
    load_capture,
    load_image_index,
    load_image_request,
    lookup_request,
    prepare,
    resolve_hotkey,
    unpack_source,
)
from validation_worker.request_replay import EvidenceUnavailable, PreparedReplay


class Stored:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.reads: list[str] = []
        self.listings: list[str] = []

    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        self.reads.append(key)
        return self.values.get(key)

    def keys(self, prefix: str) -> list[str]:
        self.listings.append(prefix)
        return [key for key in self.values if key.startswith(prefix)]


@pytest.fixture
def saved_renders(tmp_path: Path) -> tuple[SavedRenderer, Stored, Stored]:
    prefix = f"isolated/rounds/{SCOPE.round}/{SCOPE.hotkey}/"
    png = b"\x89PNG\r\n\x1a\noriginal bytes"
    previews = Stored(
        {
            prefix + "renders/v2/34eb735cb4dcc018fc4540df7cf2ea6fbb54e154bc2c3ea5df18eb23545a2296.png": png,
            prefix + "renders/v2/5eb1cb8946c1073010eb5d96ed5add40b8b6cdb06f8fd06c48caf03ea74eb4db.png": png,
            prefix
            + "renders/v2/2f364701670a2fb78ced36cb333d1c77d6cb69042d94a316a7b1688edaa48164.rejection.json": (
                b'{"error":"invalid_geometry","failures":[]}'
            ),
        }
    )
    artifacts = Stored({prefix + "submitted/one/grid.png": png + b"final"})
    renderer = SavedRenderer(
        previews,
        artifacts,
        SCOPE,
        cache=tmp_path / "previews",
        artifact_cache=tmp_path / "finals",
        storage_prefix="isolated/",
    )
    return renderer, previews, artifacts


@pytest.mark.parametrize("source,lighting", [("candidate", "neutral"), ("scene π\n", "studio")])
def test_saved_render_returns_exact_png_and_reuses_it_without_r2_reads(
    saved_renders: tuple[SavedRenderer, Stored, Stored], source: str, lighting: str
) -> None:
    renderer, previews, artifacts = saved_renders
    options = RenderOptions(lighting=lighting)
    expected = renderer.render(source, options)
    assert expected == b"\x89PNG\r\n\x1a\noriginal bytes"
    previews.values.clear()
    reads = list(previews.reads)
    assert renderer.render(source, options) == expected
    assert previews.reads == reads and not artifacts.reads


def test_saved_rejection_reaches_the_miner_and_reuses_the_cached_outcome(
    saved_renders: tuple[SavedRenderer, Stored, Stored],
) -> None:
    renderer, previews, artifacts = saved_renders
    for _ in range(2):
        with pytest.raises(RenderRejected, match="invalid_geometry") as rejected:
            renderer.render("invalid")
        assert rejected.value.detail_json == '{"error":"invalid_geometry","failures":[]}'
        if previews.values:
            previews.values.clear()
            reads = list(previews.reads)
    assert previews.reads == reads and not artifacts.reads


@pytest.mark.parametrize("change", ["source", "options"])
def test_saved_render_requires_matching_source_and_options(
    saved_renders: tuple[SavedRenderer, Stored, Stored], change: str
) -> None:
    renderer, _, _ = saved_renders
    with pytest.raises(RenderUnavailable, match="No saved render"):
        renderer.render(
            "candidate\n" if change == "source" else "candidate",
            RenderOptions(lighting="studio" if change == "options" else "neutral"),
        )


def test_saved_final_grid_uses_final_storage_and_requires_the_saved_source(
    saved_renders: tuple[SavedRenderer, Stored, Stored],
) -> None:
    renderer, previews, artifacts = saved_renders
    views = f"isolated/rounds/{SCOPE.round}/{SCOPE.hotkey}/submitted/one"
    assert renderer.final_render("one", "saved source", "saved source", views) == (
        b"\x89PNG\r\n\x1a\noriginal bytesfinal"
    )
    assert not previews.reads and artifacts.reads == [views + "/grid.png"]
    artifacts.values.clear()
    assert renderer.final_render("one", "saved source", "saved source", views).endswith(b"final")
    with pytest.raises(EvidenceUnavailable, match="Final source differs"):
        renderer.final_render("one", "changed source", "saved source", views)
    assert artifacts.reads == [views + "/grid.png"]


@pytest.mark.parametrize("views_available", [True, False])
def test_saved_final_grid_requires_a_completed_saved_render(
    saved_renders: tuple[SavedRenderer, Stored, Stored], views_available: bool
) -> None:
    renderer, _, artifacts = saved_renders
    artifacts.values.clear()
    views = f"isolated/rounds/{SCOPE.round}/{SCOPE.hotkey}/submitted/one" if views_available else None
    with pytest.raises(EvidenceUnavailable, match="[Ss]aved final grid"):
        renderer.final_render("one", "saved source", "saved source", views)


def archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for path, value in files.items():
            zipped.writestr("repo-commit/" + path, value)
    return buffer.getvalue()


def test_source_stages_supporting_files_without_importing_them(tmp_path: Path) -> None:
    unpack_source(archive({"main.py": "raise RuntimeError('must not execute')", "miner/data.txt": "data"}), tmp_path)
    assert (tmp_path / "miner/data.txt").read_text() == "data"
    with pytest.raises(ValueError, match="safely staged"):
        unpack_source(archive({"../escaped": "escape"}), tmp_path / "bad")
    assert not (tmp_path / "escaped").exists()


def test_index_preserves_retained_failed_calls_and_loads_requested_response(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live.failures[0] = "provider"
    live._capture(key, body, None)
    key, body = live._request(1, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    objects = Stored(
        {
            key: value.data
            for key, value in live.state.objects.items()
            if key.startswith("pod-calls/") or key.endswith(("/manifest.json", "/request.json", "/response.sse"))
        }
    )
    inventory = export_requests(objects, SCOPE, tmp_path / "inventory", PRICING, lambda *a, **k: None)
    assert {c.call.phase for c in inventory.captures.values()} == {"failed", "issued"}
    assert not any(key.endswith("response.sse") for key in objects.reads)
    item = next(c for c in inventory.captures.values() if c.call.phase == "issued")
    call = Call.model_validate(item.call.model_dump())
    saved = load_capture(
        objects,
        SCOPE,
        call,
        tmp_path / "capture",
        hashlib.sha256(PRICING).hexdigest(),
        "",
    )
    assert saved.usage == {"input_tokens": 10, "output_tokens": 2}
    assert json.loads((tmp_path / "capture/request.json").read_bytes())["input"] == "image"
    assert (tmp_path / "capture/response.sse").read_bytes() == live.captured[item.call.exchange_id].body


def test_index_cache_reuses_saved_journals_and_refreshes_updates(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items() if key.startswith("pod-calls/")})
    cache = tmp_path / "index-cache"
    first = export_requests(objects, SCOPE, tmp_path / "first", PRICING, lambda *a, **k: None, index_cache=cache)
    assert objects.listings and objects.reads

    key, body = live._request(1, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    objects.values.update(
        {key: value.data for key, value in live.state.objects.items() if key.startswith("pod-calls/")}
    )
    objects.reads.clear()
    objects.listings.clear()
    (tmp_path / "first/pricing.json").write_bytes(b"cached pricing was changed")
    second = export_requests(objects, SCOPE, tmp_path / "second", PRICING, lambda *a, **k: None, index_cache=cache)
    assert second == first
    assert not objects.listings and not objects.reads
    assert (tmp_path / "second/pricing.json").read_bytes() == PRICING
    for journal in (tmp_path / "first/journals").glob("*.json"):
        assert (tmp_path / "second/journals" / journal.name).read_bytes() == journal.read_bytes()

    refreshed = export_requests(
        objects, SCOPE, tmp_path / "refreshed", PRICING, lambda *a, **k: None, index_cache=cache, refresh_index=True
    )
    assert len(refreshed.captures) == len(first.captures) + 1
    assert objects.listings and objects.reads
    assert len(first.captures) == 1


def test_response_cache_reuses_original_evidence_without_remote_reads(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    response = live._capture(key, body, None)
    record = live.records.load(SCOPE, key.id)
    assert record is not None
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    pricing = hashlib.sha256(PRICING).hexdigest()
    first = load_capture(objects, SCOPE, record.call, tmp_path / "first", pricing, "", cache=tmp_path / "cache")
    assert len(objects.reads) == 3
    objects.reads.clear()
    objects.values.clear()
    second = load_capture(objects, SCOPE, record.call, tmp_path / "second", pricing, "", cache=tmp_path / "cache")
    assert second == first and not objects.reads
    assert (tmp_path / "second/response.sse").read_bytes() == response.body
    assert (tmp_path / "second/request.json").read_bytes() == body


@pytest.mark.parametrize("name", ["manifest.json", "request.json", "response.sse"])
def test_response_cache_revalidates_cached_hashes(tmp_path: Path, name: str) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    record = live.records.load(SCOPE, key.id)
    assert record is not None
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    pricing = hashlib.sha256(PRICING).hexdigest()
    cache = tmp_path / "cache"
    load_capture(objects, SCOPE, record.call, tmp_path / "first", pricing, "", cache=cache)
    cached = next(cache.rglob(name))
    if name == "manifest.json":
        value = json.loads(cached.read_bytes())
        value["usage"] = {"input_tokens": 0, "output_tokens": 0}
        cached.write_bytes(canonical(value))
    elif name == "request.json":
        cached.write_bytes(canonical({"model": "fixture", "input": "changed"}))
    else:
        cached.write_bytes(b"changed response")
    objects.reads.clear()
    with pytest.raises(ValueError, match="binding differs"):
        load_capture(objects, SCOPE, record.call, tmp_path / "second", pricing, "", cache=cache)
    assert not objects.reads


def test_response_cache_does_not_reuse_another_manifest_version(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    record = live.records.load(SCOPE, key.id)
    assert record is not None
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    pricing = hashlib.sha256(PRICING).hexdigest()
    cache = tmp_path / "cache"
    first = load_capture(objects, SCOPE, record.call, tmp_path / "first", pricing, "", cache=cache)
    path = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{record.call.exchange_id}/manifest.json"
    manifest = json.loads(objects.values[path])
    manifest["usage"] = {"input_tokens": 11, "output_tokens": 3}
    objects.values[path] = canonical(manifest)
    updated = record.call.model_copy(update={"manifest_sha256": hashlib.sha256(objects.values[path]).hexdigest()})
    objects.reads.clear()
    second = load_capture(objects, SCOPE, updated, tmp_path / "second", pricing, "", cache=cache)
    assert objects.reads and second.usage == {"input_tokens": 11, "output_tokens": 3}
    assert first.usage == {"input_tokens": 10, "output_tokens": 2}


def test_response_cache_imports_evidence_from_an_existing_run(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    response = live._capture(key, body, None)
    record = live.records.load(SCOPE, key.id)
    assert record is not None
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    pricing = hashlib.sha256(PRICING).hexdigest()
    old = tmp_path / "old-bundle"
    old.mkdir()
    first = load_capture(objects, SCOPE, record.call, old / record.call.exchange_id, pricing, "")
    objects.reads.clear()
    objects.values.clear()
    second = load_capture(
        objects, SCOPE, record.call, tmp_path / "new", pricing, "", cache=tmp_path / "cache", previous=old
    )
    assert second == first and not objects.reads
    assert (tmp_path / "new/response.sse").read_bytes() == response.body
    assert (
        load_capture(objects, SCOPE, record.call, tmp_path / "another", pricing, "", cache=tmp_path / "cache") == first
    )
    assert not objects.reads


@pytest.mark.parametrize("change", [None, "missing", "scope"])
def test_direct_request_lookup_checks_journal_identity(tmp_path: Path, change: str | None) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    captured = live._capture(key, body, None)
    journal = live.records.key(SCOPE, key.id)
    raw = live.state.objects[journal].data
    values = {journal: raw}
    if change == "missing":
        values.clear()
    elif change == "scope":
        record = json.loads(raw)
        record["scope_key"] = "another-scope"
        values[journal] = canonical(record)
    if change == "scope":
        with pytest.raises(ValueError, match="another request or scope"):
            lookup_request(Stored(values), SCOPE, key, tmp_path, "")
        return
    result = lookup_request(Stored(values), SCOPE, key, tmp_path, "")
    if change == "missing":
        assert result == {}
    else:
        assert result[captured.exchange_id].call.request == key
        assert (tmp_path / "journals" / (key.id + ".json")).read_bytes() == raw


@pytest.mark.parametrize("failures", [1, 4, 5])
def test_r2_body_timeout_restarts_the_complete_read_and_closes_each_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    payload = b"complete saved evidence"
    delays: list[float] = []

    class Body(io.BytesIO):
        def __init__(self, failing: bool) -> None:
            super().__init__(payload)
            self.failing = failing

        def read(self, amount: int | None = -1) -> bytes:
            if self.failing:
                with HTTPConnectionPool("r2.example") as pool:
                    raise SocketReadTimeout(pool, None, "response body stalled")
            return super().read(amount)

    bodies: list[Body] = []

    class Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            body = Body(len(bodies) < failures)
            bodies.append(body)
            return {"Body": StreamingBody(body, len(payload)), "ETag": '"fixture"'}

        def put_object(self, **kwargs: object) -> dict[str, object]:
            raise AssertionError("Evidence reads cannot write to R2")

    monkeypatch.setattr("time.sleep", delays.append)
    objects = R2Evidence(R2StateStore(Client(), "fixture"), lambda prefix: [])
    if failures == 5:
        with pytest.raises(ReadTimeoutError):
            objects.read("request.json")
    else:
        assert objects.read("request.json") == payload
    assert len(bodies) == min(failures + 1, 5)
    assert all(body.closed for body in bodies)
    assert delays == [1, 2, 4, 8][: min(failures, 4)]


@pytest.mark.parametrize("failure_name", ["status.json", "failure.json"])
@pytest.mark.parametrize("change", [None, "request", "status", "missing"])
def test_capture_failure_requires_bound_request_and_saved_failure_status(
    tmp_path: Path, change: str | None, failure_name: str
) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live.failures[0] = "unknown"
    with pytest.raises(WitnessError):
        live._capture(key, body, None)
    record = live.records.load(SCOPE, key.id)
    assert record is not None
    call = record.call
    prefix = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{call.exchange_id}/"
    values = {
        prefix + "request.json": body,
        prefix + failure_name: canonical({"status": "failed", "error_type": call.failure}),
    }
    previous = None
    if failure_name == "failure.json":
        values[prefix + "status.json"] = canonical({"status": "running"})
        if change is None:
            previous = tmp_path / "previous"
            old = previous / call.exchange_id
            old.mkdir(parents=True)
            (old / "request.json").write_bytes(body)
            (old / "status.json").write_bytes(values[prefix + "status.json"])
    if change == "request":
        values[prefix + "request.json"] = canonical({"model": "fixture", "input": "changed"})
    elif change == "status":
        values[prefix + failure_name] = canonical({"status": "failed", "error_type": "different"})
        if failure_name == "failure.json":
            values[prefix + "status.json"] = canonical({"status": "failed", "error_type": call.failure})
    elif change == "missing":
        del values[prefix + failure_name]
    if change is not None:
        with pytest.raises((ValueError, FileNotFoundError), match="binding differs|evidence is missing"):
            load_capture(Stored(values), SCOPE, call, tmp_path / "capture", hashlib.sha256(PRICING).hexdigest(), "")
        return
    objects = Stored(values)
    saved = load_capture(
        objects,
        SCOPE,
        call,
        tmp_path / "capture",
        hashlib.sha256(PRICING).hexdigest(),
        "",
        cache=tmp_path / "cache",
        previous=previous,
    )
    assert saved.model == "fixture" and saved.call.failure == "Interrupted"
    assert saved.usage is None and saved.response_sha256 is None and saved.provider_status is None
    assert (tmp_path / "capture/request.json").read_bytes() == body
    objects.reads.clear()
    objects.values.clear()
    assert (
        load_capture(
            objects, SCOPE, call, tmp_path / "reused", hashlib.sha256(PRICING).hexdigest(), "", cache=tmp_path / "cache"
        )
        == saved
    )
    assert not objects.reads


def test_png_request_index_reuses_verified_comparisons_without_redownloading(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    message: EasyInputMessageParam = {
        "role": "user",
        "content": [{"type": "input_image", "image_url": url, "detail": "auto"}],
    }
    key, body = live._request(0, None, {"model": "fixture", "input": [message]})
    live._capture(key, body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    inventory = export_requests(objects, SCOPE, tmp_path / "inventory", PRICING, lambda *a, **k: None)
    cache = tmp_path / "images"
    objects.reads.clear()
    first = load_image_index(objects, SCOPE, inventory, 0, None, cache, "", lambda *a, **k: None)
    assert first.requests[key.request_sha256] is not None
    assert len(objects.reads) == 1
    objects.reads.clear()
    assert load_image_index(objects, SCOPE, inventory, 0, None, cache, "", lambda *a, **k: None) == first
    assert not objects.reads
    call = next(item.call for item in inventory.captures.values() if item.call.request == key)
    assert load_image_request(objects, SCOPE, call, cache, "") == body
    assert not objects.reads
    (cache / (key.request_sha256 + ".request.json")).write_bytes(canonical({"model": "fixture", "input": "changed"}))
    with pytest.raises(ValueError, match="Recorded request digest differs"):
        load_image_request(objects, SCOPE, call, cache, "")


def test_png_request_index_rejects_a_changed_recorded_request(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    key, body = live._request(0, None, {"model": "fixture", "input": "original"})
    live._capture(key, body, None)
    objects = Stored({key: value.data for key, value in live.state.objects.items()})
    inventory = export_requests(objects, SCOPE, tmp_path / "inventory", PRICING, lambda *a, **k: None)
    for path in objects.values:
        if path.endswith("/request.json"):
            objects.values[path] = b'{"model":"fixture","input":"tampered"}'
    with pytest.raises(ValueError, match="Recorded request digest differs"):
        load_image_index(objects, SCOPE, inventory, 0, None, tmp_path / "images", "", lambda *a, **k: None)


@pytest.mark.parametrize("change", ["round", "prefix", "pricing"])
def test_index_cache_does_not_reuse_another_scope_or_pricing(tmp_path: Path, change: str) -> None:
    objects = Stored({})
    cache = tmp_path / "index-cache"
    export_requests(objects, SCOPE, tmp_path / "first", PRICING, lambda *a, **k: None, index_cache=cache)
    objects.listings.clear()
    scope = Scope(SCOPE.deployment, "test-1", SCOPE.hotkey) if change == "round" else SCOPE
    pricing = PRICING + b"\n" if change == "pricing" else PRICING
    prefix = "private/" if change == "prefix" else ""
    inventory = export_requests(
        objects,
        scope,
        tmp_path / "second",
        pricing,
        lambda *a, **k: None,
        index_cache=cache,
        storage_prefix=prefix,
    )
    assert objects.listings
    assert inventory.scope_key == scope.key
    assert inventory.pricing_sha256 == hashlib.sha256(pricing).hexdigest()


class RoundSelection(TypedDict):
    repo: str
    round_name: str
    hotkey: str
    deployment: str


def round_fixture() -> tuple[Stored, dict[str, object], RoundSelection]:
    repo = "owner/competition"
    round_name = "test-0"
    hotkey = SCOPE.hotkey
    digest = hashlib.sha256(b"PNG").hexdigest()
    image_key = "private/competition-inputs/v1/" + digest
    inputs = RoundInputs(
        repository=repo,
        branch="main",
        revision="a" * 40,
        round=round_name,
        number=0,
        seed=42,
        prompts=(
            PromptImage(id="one", sha256=digest, image_key=image_key, source_url="https://prompts.example/one.png"),
        ),
        generation_deadline_block=100,
        storage_prefix="private/",
    )
    identity = hashlib.sha256(json_bytes([repo, "main"])).hexdigest()
    root = f"private/rounds/{round_name}/{hotkey}/"
    objects = Stored(
        {
            f"pod-gateway/round-inputs/v1/{identity}/{round_name}.json": inputs.model_dump_json().encode(),
            image_key: b"PNG",
            root + "submitted.json": canonical({"one": {"failure_reason": "generation_failed"}}),
        }
    )
    files: dict[str, object] = {
        "config.json": {
            "name": "test",
            "description": "fixture",
            "first_evaluation_date": "2026-10-01",
            "last_competition_date": "2026-12-01",
            "generation_stage_minutes": 60,
            "win_margin": 0.05,
            "prompts_per_round": 1,
            "carryover_prompts": 0,
        },
        "rounds/0/submissions.json": {
            hotkey: {
                "repo": "owner/miner",
                "commit": "b" * 40,
                "revealed_at_block": 1,
                "tie_breaker": "a" * 128,
                "round": round_name,
            }
        },
        "rounds/0/schedule.json": {
            "earliest_reveal_block": 1,
            "latest_reveal_block": 2,
            "generation_deadline_block": 100,
            "witness_storage_prefix": "private/",
        },
        "rounds/0/seed.json": {"seed": 42},
    }
    return objects, files, {"repo": repo, "round_name": round_name, "hotkey": hotkey, "deployment": SCOPE.deployment}


@pytest.mark.parametrize("case", ["full", "short", "other_round", "ambiguous", "missing", "empty"])
def test_resolve_hotkey_from_frozen_round_submissions(case: str) -> None:
    _, files, settings = round_fixture()
    entries = files["rounds/0/submissions.json"]
    assert isinstance(entries, dict)
    submissions = {key: MinerSubmission.model_validate(value) for key, value in entries.items()}
    requested = settings["hotkey"] if case == "full" else settings["hotkey"][:10]
    other_hotkey = settings["hotkey"][:-1] + "6"
    if case in ("full", "other_round", "ambiguous"):
        submission = submissions[settings["hotkey"]]
        submissions[other_hotkey] = (
            submission.model_copy(update={"round": "test-1"}) if case == "other_round" else submission
        )
    if case in ("missing", "empty"):
        requested = "6" if case == "missing" else ""
    if case in ("ambiguous", "missing", "empty"):
        message = "ambiguous" if case == "ambiguous" else "No frozen source submission matches hotkey"
        with pytest.raises(ValueError, match=message) as error:
            resolve_hotkey(requested, submissions, settings["round_name"])
        if case == "ambiguous":
            assert settings["hotkey"] in str(error.value) and other_hotkey in str(error.value)
        return
    assert resolve_hotkey(requested, submissions, settings["round_name"]) == settings["hotkey"]


@pytest.mark.parametrize("outcome", ["none", "javascript", "missing", "changed_image"])
def test_prepare_resolves_frozen_source_and_independent_output(tmp_path: Path, outcome: str) -> None:
    objects, files, settings = round_fixture()
    root = f"private/rounds/test-0/{SCOPE.hotkey}/"
    if outcome == "javascript":
        objects.values[root + "submitted.json"] = canonical({"one": {"js": root + "submitted/one.js", "size": 4}})
        objects.values[root + "submitted/one.js"] = b"JS\r\n"
    elif outcome == "missing":
        objects.values[root + "submitted.json"] = b"{}"
    elif outcome == "changed_image":
        objects.values[next(key for key in objects.values if "competition-inputs/" in key)] = b"changed"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        path = request.url.path
        if path == "/repos/owner/competition/commits/heads/main":
            return httpx.Response(200, json={"sha": "a" * 40})
        if path.startswith("/repos/owner/competition/contents/"):
            name = path.removeprefix("/repos/owner/competition/contents/")
            if name == "rounds/0/prompts.txt":
                return httpx.Response(200, text="https://prompts.example/one.png\n")
            return httpx.Response(200, json=files[name]) if name in files else httpx.Response(404)
        if path == "/repos/owner/miner/git/commits/" + "b" * 40:
            return httpx.Response(200, json={"sha": "b" * 40})
        if path == "/repos/owner/miner/zipball/" + "b" * 40:
            return httpx.Response(200, content=archive({"main.py": "def generate(*args): return None"}))
        raise AssertionError(path)

    async def run() -> PreparedReplay:
        async with httpx.AsyncClient(base_url="https://github.test", transport=httpx.MockTransport(handler)) as http:
            git = GitHubClient.with_client(http, settings["repo"])
            return await prepare(
                git=git,
                objects=objects,
                output=tmp_path,
                pricing=PRICING,
                emit=lambda *a, **k: None,
                round_name=settings["round_name"],
                hotkey=settings["hotkey"],
                deployment=settings["deployment"],
            )

    if outcome == "changed_image":
        with pytest.raises(ValueError, match="image hash differs"):
            asyncio.run(run())
        return
    manifest = asyncio.run(run())
    expected = json.loads((tmp_path / "prompts/one/expected.json").read_bytes())
    assert manifest.source_commit == "b" * 40
    assert manifest.seed == 42
    assert expected["available"] == (outcome != "missing")
    if outcome != "missing":
        assert expected["output"] == ("JS\r\n" if outcome == "javascript" else None)
    assert all(request.url.params.get("ref") == "a" * 40 for request in requests if "/contents/" in request.url.path)
