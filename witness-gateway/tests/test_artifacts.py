"""Artifact storage through authenticated local services; no provider or R2 traffic."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from renderer_client import ArtifactClient, ArtifactUnavailable, RenderOptions, RenderRejected, RenderUnavailable
from support import MemoryState
from test_service import Harness
from witness_client import WitnessError

from pod_gateway.artifacts import (
    ArtifactConflict,
    ArtifactLimit,
    ArtifactLimits,
    ArtifactPending,
    ArtifactProfile,
    ArtifactRepository,
    SubmissionContext,
)
from pod_gateway.models import Scope
from pod_gateway.state import Snapshot, WriteRejected

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN3sAAAAASUVORK5CYII=")
SOURCE = "export default function generate(THREE) { return new THREE.Mesh(new THREE.BoxGeometry(.5,.5,.5)); }"
PROFILE = ArtifactProfile(image_digest="sha256:" + "a" * 64, validation_sha256="b" * 64)
LIMITS = ArtifactLimits(max_renders=16, max_bytes=128 * 1024 * 1024)
COMMIT = "c" * 40


async def enable(fixture: Harness, *, limits: ArtifactLimits = LIMITS, png: bytes = PNG) -> list[str]:
    rendered: list[str] = []

    async def backend(request: web.Request) -> web.Response:
        source = (await request.json())["source"]
        rendered.append(source)
        if source == "invalid":
            return web.json_response({"error": "validation_failed", "failures": []}, status=422)
        return web.Response(body=png, content_type="image/png")

    app = web.Application()
    app.router.add_post("/render/grid", backend)
    origin = await fixture.listen(app, None)
    runtime, service = fixture.runtimes[-1], fixture.services[-1]
    service.renderer_url = origin + "/render/grid"
    service.artifacts = ArtifactRepository(
        fixture.state,
        fixture.state,
        runtime.scope,
        PROFILE,
        limits,
        seed=42,
        submission_commit=COMMIT,
        prompt_hashes=frozenset({hashlib.sha256(fixture.image).hexdigest()}),
    )
    return rendered


def client(fixture: Harness, transport: httpx.BaseTransport, *, seed: int = 42) -> ArtifactClient:
    return ArtifactClient(
        "https://witness.internal",
        renderer_id=PROFILE.image_digest,
        round=fixture.scope.round,
        prompt_sha256=hashlib.sha256(fixture.image).hexdigest(),
        seed=seed,
        submission_commit=COMMIT,
        transport=transport,
    )


def test_runner_selects_one_candidate_and_collector_reads_exact_durable_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)
                selected = SOURCE + "\r\n// deterministic Unicode: café\r\n"

                def run() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        first = renderer.render_saved(SOURCE)
                        assert renderer.render(selected, RenderOptions(lighting="neutral")) == PNG
                        saved = renderer.save_final(selected)
                        assert saved["artifact_id"] != first["artifact_id"]
                        assert renderer.get_submission() == saved
                        # A retry selects the same bytes without uploading source or rendering.
                        assert renderer.save_final(selected) == saved

                await asyncio.to_thread(run)
                assert rendered == [SOURCE, selected] and fixture.paid == 0
                restarted = ArtifactRepository(
                    fixture.state,
                    fixture.state,
                    fixture.scope,
                    PROFILE,
                    LIMITS,
                    seed=42,
                    submission_commit=COMMIT,
                    prompt_hashes=frozenset({hashlib.sha256(fixture.image).hexdigest()}),
                )
                choice, source, artifact = restarted.collect_final(hashlib.sha256(fixture.image).hexdigest())
                assert source == selected.encode() and not choice.output_is_none
                assert artifact is not None and artifact.png is not None
                assert restarted.read_object(artifact.png, 32 * 1024 * 1024) == PNG
                (tmp_path / "selected.js").write_bytes(source)
                (tmp_path / "preview.png").write_bytes(PNG)
                (tmp_path / "submission.json").write_text(choice.model_dump_json(indent=2))
                (tmp_path / "render.json").write_text(artifact.model_dump_json(indent=2))

    asyncio.run(scenario())


@pytest.mark.parametrize("output", [None, SOURCE, SOURCE + "\n// edited"])
def test_none_and_unrendered_returns_do_not_select_the_previous_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str | None,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)

                def run() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        renderer.render_saved(SOURCE + "\n// discarded")
                        result = renderer.save_final(output)
                        assert result["output_is_none"] == (output is None)

                await asyncio.to_thread(run)
                store = fixture.services[0].artifacts
                assert store is not None
                _, source, _ = store.collect_final(hashlib.sha256(fixture.image).hexdigest())
                assert source == (None if output is None else output.encode())
                assert len(rendered) == (1 if output is None else 2)

    asyncio.run(scenario())


def test_lost_acknowledgements_do_not_generate_or_render_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from miner_reference.submissions import run_and_save

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)
                original = witness.renderer_transport()
                lost: set[str] = set()
                generated: list[str] = []

                def drop_ack(request: httpx.Request) -> httpx.Response:
                    reply = original.handle_request(request)
                    if (
                        request.url.path not in lost
                        and request.url.path in ("/artifacts/render", "/submissions")
                        and reply.is_success
                    ):
                        lost.add(request.url.path)
                        reply.close()
                        raise httpx.ReadError("Fixture lost acknowledgement", request=request)
                    return reply

                def run() -> None:
                    with client(fixture, httpx.MockTransport(drop_ack)) as renderer:

                        def generate() -> str:
                            generated.append(SOURCE)
                            return SOURCE

                        for _ in range(3):
                            try:
                                saved = run_and_save(generate, renderer, tmp_path / "final.json")
                                break
                            except RenderUnavailable:
                                pass
                        else:
                            pytest.fail("Final save did not recover")
                        assert renderer.get_submission() == saved

                await asyncio.to_thread(run)
                assert generated == [SOURCE] and rendered == [SOURCE] and fixture.paid == 0

    asyncio.run(scenario())


def test_rejection_is_saved_and_remains_a_rejection_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)

                def run() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        with pytest.raises(RenderRejected):
                            renderer.render("invalid")
                        renderer.save_final("invalid")
                        with pytest.raises(RenderRejected):
                            renderer.render("invalid")

                await asyncio.to_thread(run)
                restarted = ArtifactRepository(
                    fixture.state,
                    fixture.state,
                    fixture.scope,
                    PROFILE,
                    LIMITS,
                    seed=42,
                    submission_commit=COMMIT,
                    prompt_hashes=frozenset({hashlib.sha256(fixture.image).hexdigest()}),
                )
                _, source, artifact = restarted.collect_final(hashlib.sha256(fixture.image).hexdigest())
                assert source == b"invalid" and artifact is not None
                assert artifact.validation == "rejected" and artifact.png is None
                assert rendered == ["invalid"]

    asyncio.run(scenario())


def test_cutoff_allows_saved_result_recovery_and_blocks_new_choices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)

                def before() -> dict[str, object]:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        return renderer.save_final(SOURCE)

                saved = await asyncio.to_thread(before)
                fixture.services[0].round_end = 0

                def after() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        assert renderer.get_submission() == saved
                        assert renderer.save_final(SOURCE) == saved
                        assert renderer.render(SOURCE) == PNG
                        with pytest.raises(ArtifactUnavailable) as closed:
                            renderer.save_final(None, expected_revision=str(saved["revision"]))
                        assert closed.value.status == 403 and closed.value.code == "round_closed"

                await asyncio.to_thread(after)
                assert rendered == [SOURCE]

    asyncio.run(scenario())


def test_large_saved_png_uses_render_limit_not_evidence_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large = PNG + b"x" * (17 * 1024 * 1024)

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture, png=large)

                def run() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        renderer.render_saved(SOURCE)
                        assert renderer.render(SOURCE) == large

                await asyncio.to_thread(run)

    asyncio.run(scenario())


def test_explicit_replacement_rejects_stale_choices_and_reuses_only_exact_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)

                def run() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        studio = renderer.render_saved(SOURCE)
                        neutral = renderer.render_saved(SOURCE, RenderOptions(lighting="neutral"))
                        assert studio["artifact_id"] != neutral["artifact_id"]
                        assert renderer.render(SOURCE) == PNG
                        first = renderer.save_final(SOURCE)
                        with pytest.raises(ArtifactUnavailable) as conflict:
                            renderer.save_final(None)
                        assert conflict.value.status == 409
                        second = renderer.save_final(None, expected_revision=str(first["revision"]))
                        assert second["output_is_none"]
                        with pytest.raises(ArtifactUnavailable) as stale:
                            renderer.save_final(SOURCE)
                        assert stale.value.status == 409
                        assert renderer.get_submission() == second

                await asyncio.to_thread(run)
                assert rendered == [SOURCE, SOURCE]

    asyncio.run(scenario())


def test_failed_upload_is_not_a_saved_render_and_is_not_dispatched_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)
                original = fixture.state.save

                def fail_manifest(key: str, data: bytes, expected: str | None) -> Snapshot:
                    if "/renders/" in key:
                        raise OSError("Fixture object upload failed")
                    return original(key, data, expected)

                monkeypatch.setattr(fixture.state, "save", fail_manifest)

                def run() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        for _ in range(2):
                            with pytest.raises(ArtifactUnavailable) as pending:
                                renderer.save_final(SOURCE)
                            assert pending.value.status == 503

                await asyncio.to_thread(run)
                store = fixture.services[0].artifacts
                assert store is not None and store.load_final(hashlib.sha256(fixture.image).hexdigest()) is None
                assert rendered == [SOURCE]

    asyncio.run(scenario())


def test_concurrent_same_candidate_has_one_render_and_quota_blocks_extra_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture, limits=ArtifactLimits(max_renders=1, max_bytes=128 * 1024 * 1024))

                def render() -> dict[str, object] | None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        try:
                            return renderer.render_saved(SOURCE)
                        except ArtifactUnavailable as error:
                            assert error.status == 503 and error.code == "artifact_pending"
                            return None

                outcomes = await asyncio.gather(asyncio.to_thread(render), asyncio.to_thread(render))
                assert any(result is not None for result in outcomes)
                assert await asyncio.to_thread(render) is not None

                def exceed() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        with pytest.raises(ArtifactUnavailable) as limit:
                            renderer.render_saved(SOURCE + "\n// other")
                        assert limit.value.status == 429 and limit.value.code == "artifact_storage_limit"

                await asyncio.to_thread(exceed)
                assert rendered == [SOURCE]

    asyncio.run(scenario())


def test_pod_replacement_recovers_choice_and_denies_stale_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                old_renders = await enable(fixture)
                stale_service = witness._gateway._service()

                def save() -> dict[str, object]:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        return renderer.save_final(SOURCE)

                saved = await asyncio.to_thread(save)
                old = fixture.provider.list()[0]
                fixture.selection.update(old, active=False)
                fixture.provider.delete(old.id)
                witness._gateway._expires = 0
                await asyncio.to_thread(witness._gateway.ensure_pod)
                assert fixture.provider.creates == 2 and len(fixture.runtimes) == 2
                new_renders = await enable(fixture)

                def recover() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        assert renderer.get_submission() == saved
                        assert renderer.save_final(SOURCE) == saved
                        assert renderer.render(SOURCE) == PNG
                    with client(fixture, httpx.MockTransport(stale_service.render)) as stale:
                        with pytest.raises(ArtifactUnavailable) as rejected:
                            stale.save_final(None, expected_revision=str(saved["revision"]))
                        assert rejected.value.status == 409 and rejected.value.code == "pod_not_selected"

                await asyncio.to_thread(recover)
                assert old_renders == [SOURCE] and new_renders == [] and fixture.paid == 0

    asyncio.run(scenario())


def repository(state: MemoryState, *, miner: str = "2" * 48) -> ArtifactRepository:
    return ArtifactRepository(
        state,
        state,
        Scope("test", "test-0", miner),
        PROFILE,
        LIMITS,
        seed=42,
        submission_commit=COMMIT,
        prompt_hashes=frozenset({hashlib.sha256(b"prompt").hexdigest()}),
    )


def completed(store: ArtifactRepository, generation: SubmissionContext, source: str) -> None:
    options = RenderOptions()
    owner = store.reserve(generation, source, options)
    artifact = store.complete(generation, source, options, owner=owner, png=PNG)
    store.release_reservation(artifact)


def test_concurrent_choices_conflict_and_foreign_artifacts_cannot_be_selected() -> None:
    state = MemoryState()
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    sources = [SOURCE, SOURCE + "\n// other"]
    for source in sources:
        completed(store, generation, source)

    def select(source: str) -> bool:
        digest = hashlib.sha256(source.encode()).hexdigest()
        try:
            store.save_final(generation, digest, None, store.find_render(generation, digest))
            return True
        except ArtifactConflict:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(select, sources)) == [False, True]
    _, selected, artifact = store.collect_final(generation.prompt_sha256)
    assert selected in [s.encode() for s in sources] and artifact is not None
    foreign = repository(state, miner="3" * 48)
    assert foreign.find_render(generation, artifact.source.sha256) is None
    with pytest.raises(ArtifactConflict):
        foreign.save_final(generation, artifact.source.sha256, None, artifact)
    assert foreign.load_final(generation.prompt_sha256) is None


def test_pending_render_and_quotas_survive_restart_without_reexecution() -> None:
    state = MemoryState()
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    limits = ArtifactLimits(max_renders=1, max_bytes=128 * 1024 * 1024)
    first = ArtifactRepository(
        state,
        state,
        Scope("test", "test-0", "2" * 48),
        PROFILE,
        limits,
        seed=42,
        submission_commit=COMMIT,
        prompt_hashes=frozenset({hashlib.sha256(b"prompt").hexdigest()}),
    )
    first.reserve(generation, SOURCE, RenderOptions())
    restarted = ArtifactRepository(
        state,
        state,
        first.scope,
        PROFILE,
        limits,
        seed=42,
        submission_commit=COMMIT,
        prompt_hashes=frozenset({hashlib.sha256(b"prompt").hexdigest()}),
    )
    with pytest.raises(ArtifactPending):
        restarted.reserve(generation, SOURCE, RenderOptions())
    with pytest.raises(ArtifactLimit):
        restarted.reserve(generation, SOURCE + "\n// new", RenderOptions())


def test_changed_stored_source_stays_unresolved() -> None:
    state = MemoryState()
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    completed(store, generation, SOURCE)
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    artifact = store.find_render(generation, digest)
    assert artifact is not None
    state.put(artifact.source.key, b"changed")
    with pytest.raises(ArtifactPending):
        store.save_final(generation, digest, None, artifact)
    assert store.load_final(generation.prompt_sha256) is None


def test_interrupted_render_recovers_after_lease_and_never_replaces_observed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MemoryState()
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    now = [1000.0]
    monkeypatch.setattr("pod_gateway.artifacts.time.time", lambda: now[0])
    first = store.reserve(generation, SOURCE, RenderOptions())
    with pytest.raises(ArtifactPending):
        store.reserve(generation, SOURCE, RenderOptions())
    now[0] += 301
    restarted = repository(state)
    owner = restarted.reserve(generation, SOURCE, RenderOptions())
    with pytest.raises(ArtifactPending, match="ownership changed"):
        store.complete(generation, SOURCE, RenderOptions(), owner=first, png=PNG + b"old")
    saved = restarted.complete(generation, SOURCE, RenderOptions(), owner=owner, png=PNG)
    restarted.release_reservation(saved)
    assert store.complete(generation, SOURCE, RenderOptions(), owner=first, png=PNG + b"old") == saved
    assert saved.png is not None and restarted.read_object(saved.png, 32 * 1024 * 1024) == PNG


def test_storage_throttling_preserves_conditional_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    state = MemoryState()
    original = state.save
    throttled: set[str] = set()

    def save(key: str, data: bytes, expected: str | None) -> Snapshot:
        if key.endswith(".json") and key not in throttled:
            throttled.add(key)
            raise WriteRejected("Fixture write rate limit")
        return original(key, data, expected)

    monkeypatch.setattr(state, "save", save)
    monkeypatch.setattr("pod_gateway.artifacts.time.sleep", lambda _: None)
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    completed(store, generation, SOURCE)
    source_hash = hashlib.sha256(SOURCE.encode()).hexdigest()
    saved = store.save_final(generation, source_hash, None, store.find_render(generation, source_hash))
    assert store.collect_final(generation.prompt_sha256)[1] == SOURCE.encode()
    assert store.save_final(generation, source_hash, None, store.find_render(generation, source_hash)) == saved


def test_frozen_seed_and_commit_cannot_change_during_recovery() -> None:
    state = MemoryState()
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    completed(store, generation, SOURCE)
    with pytest.raises(ValueError, match="seed differs"):
        store.reserve(generation.model_copy(update={"seed": 43}), SOURCE, RenderOptions())
    changed = ArtifactRepository(
        state,
        state,
        store.scope,
        PROFILE,
        LIMITS,
        seed=42,
        submission_commit="d" * 40,
        prompt_hashes=frozenset({hashlib.sha256(b"prompt").hexdigest()}),
    )
    with pytest.raises(ArtifactConflict):
        changed.reserve(generation, SOURCE + "\n// new", RenderOptions())


def test_non_admitted_registered_miner_cannot_allocate_a_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            fixture.membership.admitted_hotkeys = frozenset()
            with fixture.client() as witness:
                with pytest.raises(WitnessError) as rejected:
                    await asyncio.to_thread(witness._gateway.ensure_pod)
                assert rejected.value.status_code == 403 and rejected.value.code == "hotkey_not_admitted"
                assert fixture.provider.creates == 0
                fixture.membership.admitted_hotkeys = frozenset({fixture.scope.hotkey})
                await asyncio.to_thread(witness._gateway.ensure_pod)
                assert fixture.provider.creates == 1

    asyncio.run(scenario())


def test_bounded_rejection_survives_manifest_json_escaping() -> None:
    state = MemoryState()
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    owner = store.reserve(generation, SOURCE, RenderOptions())
    rejection = json.dumps({"error": chr(92) * 20000})
    saved = store.complete(generation, SOURCE, RenderOptions(), owner=owner, rejection_json=rejection)
    store.release_reservation(saved)
    recovered = repository(state).find_render(generation, hashlib.sha256(SOURCE.encode()).hexdigest())
    assert recovered is not None and recovered.rejection_json == rejection and recovered.png is None


def test_collected_choice_is_idempotently_readable_and_cannot_be_replaced() -> None:
    state = MemoryState()
    store = repository(state)
    generation = SubmissionContext.from_prompt(b"prompt", 42, round_number="test-0")
    completed(store, generation, SOURCE)
    source_hash = hashlib.sha256(SOURCE.encode()).hexdigest()
    artifact = store.find_render(generation, source_hash)
    saved = store.save_final(generation, source_hash, None, artifact)
    key = f"{store.prefix(generation.prompt_sha256)}/final.json"
    previous = state.load(key)
    assert previous is not None
    sealed = saved.model_copy(update={"sealed": True})
    state.save(key, sealed.model_dump_json().encode(), previous.etag)
    with pytest.raises(ArtifactConflict, match="sealed"):
        store.save_final(generation, None, saved.revision, None)
    assert store.save_final(generation, source_hash, None, artifact) == sealed


def test_runner_requires_a_private_checkpoint_directory(tmp_path: Path) -> None:
    from miner_reference.submissions import run_and_save

    generated: list[str] = []

    def generate() -> str:
        generated.append(SOURCE)
        return SOURCE

    tmp_path.chmod(0o755)
    try:
        with (
            ArtifactClient(
                "https://witness.internal",
                renderer_id=PROFILE.image_digest,
                round="test-0",
                prompt_sha256="a" * 64,
                seed=42,
                submission_commit=COMMIT,
            ) as renderer,
            pytest.raises(ValueError, match="must be private"),
        ):
            run_and_save(generate, renderer, tmp_path / "final.json")
        assert not generated
    finally:
        tmp_path.chmod(0o700)


def test_model_calls_allow_other_work_while_artifacts_require_admitted_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                assert (await asyncio.to_thread(witness.create, model="fixture", input="unrelated work")).output_text
                await enable(fixture)

                def save() -> None:
                    with client(fixture, witness.renderer_transport()) as renderer:
                        renderer.generation["prompt_sha256"] = "d" * 64
                        with pytest.raises(ArtifactUnavailable) as rejected:
                            renderer.save_final(None)
                        assert rejected.value.code == "unknown_round_prompt" and rejected.value.status == 403
                        renderer.generation["prompt_sha256"] = hashlib.sha256(fixture.image).hexdigest()
                        renderer.generation["seed"] = 43
                        with pytest.raises(ArtifactUnavailable) as rejected:
                            renderer.save_final(None)
                        assert rejected.value.status == 400
                        renderer.generation["seed"] = 42
                        assert renderer.save_final(None)["output_is_none"] is True

                await asyncio.to_thread(save)
                assert fixture.paid == 1

    asyncio.run(scenario())


def test_managed_batch_recovers_final_save_on_replacement_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miner_reference.batch import BatchSettings, Prompt, run_batch
    from miner_reference.pipeline import ModelCalls
    from renderer_client import Renderer

    generated: list[str] = []

    def generate(image: bytes, seed: int, calls: ModelCalls, renderer: Renderer) -> str:
        source = SOURCE + "\n// " + calls.create(model="fixture", input="managed output", request_id=1).output_text
        generated.append(source)
        renderer.render(source)
        return source

    monkeypatch.setattr("miner_reference.pipeline.run", generate)

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        path = tmp_path / "prompt.png"
        path.write_bytes(fixture.image)
        prompts = [Prompt("chair", path, hashlib.sha256(fixture.image).hexdigest())]
        settings = BatchSettings(tmp_path / "batch", 42, "fixture", submission_commit=COMMIT)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                old_renders = await enable(fixture)
                original = witness.renderer_transport
                lost = False

                def transport() -> httpx.BaseTransport:
                    inner = original()

                    def deliver(request: httpx.Request) -> httpx.Response:
                        nonlocal lost
                        response = inner.handle_request(request)
                        if not lost and request.url.path == "/submissions" and response.is_success:
                            lost = True
                            response.close()
                            raise httpx.ReadError("Lost final-save acknowledgement", request=request)
                        return response

                    return httpx.MockTransport(deliver)

                monkeypatch.setattr(witness, "renderer_transport", transport)
                with client(fixture, original()) as renderer:
                    result = await asyncio.to_thread(run_batch, prompts, settings, witness, renderer, progress=lambda _: None)
                    assert result == {"chair": "pending"} and lost
                    old = fixture.provider.list()[0]
                    fixture.selection.update(old, active=False)
                    fixture.provider.delete(old.id)
                    witness._gateway._expires = 0
                    await asyncio.to_thread(witness._gateway.ensure_pod)
                    new_renders = await enable(fixture)
                    result = await asyncio.to_thread(run_batch, prompts, settings, witness, renderer, progress=lambda _: None)
                    assert result == {"chair": "selected"}
                assert fixture.paid == 1 and len(generated) == 1
                assert old_renders == generated and new_renders == []
                assert (settings.output / "chair/selected.js").read_text() == generated[0]
                store = fixture.services[-1].artifacts
                assert store is not None
                assert store.collect_final(prompts[0].sha256)[1] == generated[0].encode()

    asyncio.run(scenario())
