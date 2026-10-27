"""Final saving through authenticated local services; no provider or R2 traffic."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from renderer_client import ArtifactClient, ArtifactUnavailable, RenderRejected, RenderUnavailable
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from support import MemoryState
from test_service import Harness

from pod_gateway.artifacts import ArtifactRepository
from pod_gateway.judging import Judging
from pod_gateway.models import Scope
from pod_gateway.state import Snapshot


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN3sAAAAASUVORK5CYII=")
SOURCE = "export default function generate(THREE) { return new THREE.Mesh(new THREE.BoxGeometry(.5,.5,.5)); }"
IMAGE = "sha256:" + "a" * 64
COMMIT = "c" * 40


async def enable(fixture: Harness, *, fail_first_render: bool = False, commit: str = COMMIT) -> list[str]:
    rendered: list[str] = []
    async def backend(request: web.Request) -> web.Response:
        nonlocal fail_first_render
        if fail_first_render:
            fail_first_render = False
            return web.json_response({"error": "renderer_unavailable"}, status=503)
        source = (await request.json())["source"]
        if source == "invalid":
            return web.json_response({"error": "validation_failed", "failures": []}, status=422)
        if request.path == "/render":
            count = len(request.query["thetas"].split(","))
            return web.json_response({"images": [base64.b64encode(PNG).decode()] * count})
        rendered.append(source)
        return web.Response(body=PNG, content_type="image/png")

    app = web.Application()
    app.router.add_post("/render/grid", backend)
    app.router.add_post("/render", backend)
    origin = await fixture.listen(app, None)
    runtime, service = fixture.runtimes[-1], fixture.services[-1]
    service.renderer_url = origin + "/render/grid"
    service.artifacts = ArtifactRepository(
        fixture.state,
        runtime.scope,
        index=fixture.state,
        seed=42,
        submission_commit=commit,
        prompt_hashes=frozenset({hashlib.sha256(fixture.image).hexdigest()}),
    )
    service.judging = Judging(service.artifacts, origin, "fixture-revision", None)
    fixture.monkeypatch.setattr(
        "pod_gateway.judging.build_embeddings_npz", AsyncMock(return_value=b"fixture embeddings")
    )
    return rendered


def client(fixture: Harness, transport: httpx.BaseTransport) -> ArtifactClient:
    return ArtifactClient(
        "https://witness.internal",
        renderer_id=IMAGE,
        prompt_sha256=hashlib.sha256(fixture.image).hexdigest(),
        prompt_image=fixture.image,
        seed=42,
        transport=transport,
    )


def test_runner_saves_returned_source_and_all_judging_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from miner_reference.submissions import run_and_save

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:

                    def generate() -> str:
                        assert renderer.render(SOURCE) == PNG
                        return SOURCE + "\n// final"

                    result = await asyncio.to_thread(run_and_save, generate, renderer, tmp_path / "private/final.json")
                    saved = await asyncio.to_thread(renderer.get_submission)
                    assert saved == result
                store = fixture.services[-1].artifacts
                assert store is not None
                index = GenerationsAdapter.validate_json(fixture.state.get(store.index_key) or b"{}")
                assert index[hashlib.sha256(fixture.image).hexdigest()] == GenerationResult.model_validate(result)
                root = str(result["views"])
                assert fixture.state.get(str(result["js"])) == (SOURCE + "\n// final").encode()
                assert result["size"] == len((SOURCE + "\n// final").encode()) and result["failure_reason"] is None
                for name in ("white/front_left.png", "gray/front_above.png", "grid.png"):
                    assert fixture.state.get(f"{root}/{name}") == PNG
                assert fixture.state.get(f"{root}/embeddings.npz") == b"fixture embeddings"
                assert fixture.paid == 0

    asyncio.run(scenario())


def test_final_checkpoint_resumes_after_round_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from miner_reference.submissions import run_and_save

    generated: list[str] = []

    def generate() -> str:
        generated.append(SOURCE)
        return SOURCE

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                first_renders = await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    first = await asyncio.to_thread(
                        run_and_save, generate, renderer, tmp_path / "final.json",
                    )
                    fixture.scope = Scope(fixture.scope.deployment, "test-1", fixture.scope.hotkey)
                    fixture.gateway.round = fixture.scope.round
                    witness._gateway._expires = 0
                    await asyncio.to_thread(witness._gateway.ensure_pod)
                    next_renders = await enable(fixture)
                    saved = await asyncio.to_thread(
                        run_and_save, generate, renderer, tmp_path / "final.json",
                    )
                    assert first["js"] != saved["js"]
                    assert await asyncio.to_thread(renderer.get_submission) == saved
                store = fixture.services[-1].artifacts
                assert store is not None
                assert fixture.state.get(str(saved["js"])) == SOURCE.encode() and fixture.paid == 0
                assert first_renders == next_renders == [SOURCE]
                assert generated == [SOURCE]

    asyncio.run(scenario())


def test_final_checkpoint_rejects_changed_source_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from miner_reference.submissions import run_and_save

    generated: list[str] = []

    def generate() -> str:
        generated.append(SOURCE)
        return SOURCE

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                first_renders = await enable(fixture)
                first_store = fixture.services[-1].artifacts
                assert first_store is not None

                with client(fixture, witness.renderer_transport()) as renderer:
                    def save() -> None:
                        run_and_save(generate, renderer, tmp_path / "final.json")

                    await asyncio.to_thread(save)
                    next_renders = await enable(fixture, commit="d" * 40)
                    with pytest.raises(ValueError, match="checkpoint context differs"):
                        await asyncio.to_thread(save)
                    prompt = hashlib.sha256(fixture.image).hexdigest()
                    result = first_store.result(prompt)
                    assert result is not None and fixture.state.get(str(result.js)) == SOURCE.encode()
                    assert generated == first_renders == [SOURCE] and next_renders == []
                    assert fixture.paid == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("entrypoint", ["main", "batch"])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("subscription", [False, True])
def test_reference_miner_completes_authenticated_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, recover: bool, subscription: bool,
) -> None:
    import json

    from test_recording import PRICING
    from main import generate
    from miner_reference.batch import BatchSettings, Prompt, run_batch
    from miner_reference.submissions import run_and_save
    from miner_runtime.runner import run_generation
    from renderer_client import RendererClient
    from witness_client.calls import canonical

    pricing = json.loads(PRICING)
    pricing["calls_per_prompt"] = 10
    rate = pricing["models"]["fixture"]
    pricing["models"] = {name: rate for name in ("gpt-6-astra", "gpt-6.1-sol")}
    monkeypatch.setattr("test_service.PRICING", canonical(pricing))
    if subscription:
        monkeypatch.setattr(
            "witness_client.request.subscription_credentials", lambda *_: ("fixture-" + "a" * 80, "c" * 32),
        )

    def answer(body: bytes, number: int) -> str:
        if json.loads(body).get("text", {}).get("format", {}).get("type") == "json_schema":
            return json.dumps({"winner": "A", "reason": "closest shape", "improvements": "refine proportions"})
        return SOURCE + f"\n// candidate {number}"

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.image, fixture.response_text = PNG, answer
        if recover:
            fixture.failures = ["lost"]
        path = tmp_path / "prompt.png"
        path.write_bytes(fixture.image)
        prompt = Prompt("chair", path, hashlib.sha256(fixture.image).hexdigest())
        settings = BatchSettings(tmp_path / "batch", 42, "fixture")
        async with fixture.running():
            with fixture.client(subscription=subscription) as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)
                original = witness.renderer_transport
                lost = False

                def transport() -> httpx.BaseTransport:
                    inner = original()

                    def deliver(request: httpx.Request) -> httpx.Response:
                        nonlocal lost
                        response = inner.handle_request(request)
                        if recover and not lost and request.url.path == "/v1/submissions" and response.is_success:
                            lost = True
                            response.close()
                            raise httpx.ReadError("Lost final-save acknowledgement", request=request)
                        return response

                    return httpx.MockTransport(deliver)

                monkeypatch.setattr(witness, "renderer_transport", transport)
                with RendererClient(
                    "https://witness.internal/v1", renderer_id=IMAGE, transport=transport(),
                ) as renderer, client(fixture, transport()) as submissions:
                    checkpoint = tmp_path / "private/final.json"

                    def run() -> object:
                        if entrypoint == "batch":
                            return run_batch([prompt], settings, witness, renderer, progress=lambda _: None)
                        return run_and_save(
                            lambda: run_generation(generate, fixture.image, 42, witness, renderer),
                            submissions, checkpoint,
                        )

                    if recover:
                        if entrypoint == "batch":
                            assert await asyncio.to_thread(run) == {"chair": "pending"}
                        else:
                            with pytest.raises(RenderUnavailable):
                                await asyncio.to_thread(run)
                        assert lost and fixture.paid == 7
                    result = await asyncio.to_thread(run)
                    if entrypoint == "batch":
                        assert result == {"chair": "selected"}
                        output = (settings.output / "chair/selected.js").read_text()
                    else:
                        output = json.loads(checkpoint.read_bytes())["output"]
                    saved = await asyncio.to_thread(submissions.get_submission)
                    assert fixture.state.get(str(saved["js"])) == output.encode()
                    assert saved["failure_reason"] is None
                    assert fixture.state.get(str(saved["views"]) + "/embeddings.npz") == b"fixture embeddings"
                    await asyncio.to_thread(run)
                    assert fixture.paid == 7 and len(rendered) == 6

    asyncio.run(scenario())


def test_none_is_saved_as_a_generation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    result = await asyncio.to_thread(renderer.save_final, None)
                    assert result == {"js": None, "views": None, "failure_reason": "generation_failed", "size": 0}
                    assert await asyncio.to_thread(renderer.get_submission) == result

    asyncio.run(scenario())


@pytest.mark.parametrize("output", [SOURCE, None])
def test_lost_final_acknowledgement_retries_saving_without_generating_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str | None
) -> None:
    from miner_reference.submissions import run_and_save

    generated: list[str | None] = []

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                rendered = await enable(fixture)
                inner = witness.renderer_transport()
                lost = False

                def deliver(request: httpx.Request) -> httpx.Response:
                    nonlocal lost
                    response = inner.handle_request(request)
                    if request.url.path == "/v1/submissions" and response.is_success and not lost:
                        lost = True
                        response.close()
                        raise httpx.ReadError("Lost final-save acknowledgement", request=request)
                    return response

                def generate() -> str | None:
                    generated.append(output)
                    return output

                with client(fixture, httpx.MockTransport(deliver)) as renderer:
                    checkpoint = tmp_path / "private/final.json"
                    with pytest.raises(RenderUnavailable, match="transport"):
                        await asyncio.to_thread(run_and_save, generate, renderer, checkpoint)
                    fixture.services[-1].round_end = 0
                    result = await asyncio.to_thread(run_and_save, generate, renderer, checkpoint)
                    if output is not None:
                        assert fixture.state.get(str(result["js"])) == output.encode()
                    else:
                        assert result["failure_reason"] == "generation_failed"
                assert generated == [output] and fixture.paid == 0
                assert rendered == ([] if output is None else [output])

    asyncio.run(scenario())


def test_rejected_source_reports_an_error_and_keeps_the_saved_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    with pytest.raises(RenderRejected):
                        await asyncio.to_thread(renderer.save_final, "invalid")
                    rejected = await asyncio.to_thread(renderer.get_submission)
                    assert rejected["failure_reason"] == "render_rejected" and rejected["views"] is None
                    assert fixture.state.get(str(rejected["js"])) == b"invalid"
                    assert await asyncio.to_thread(renderer.save_final, "invalid") == rejected
                    with pytest.raises(ArtifactUnavailable) as conflict:
                        await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert conflict.value.status == 409
                    assert await asyncio.to_thread(renderer.get_submission) == rejected

    asyncio.run(scenario())


@pytest.mark.parametrize("first,second", [(SOURCE, SOURCE + "\n// other"), (SOURCE, None), (None, SOURCE)])
def test_saved_final_output_cannot_be_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str | None, second: str | None
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    saved = await asyncio.to_thread(renderer.save_final, first)
                    with pytest.raises(ArtifactUnavailable) as conflict:
                        await asyncio.to_thread(renderer.save_final, second)
                    assert conflict.value.status == 409
                    assert await asyncio.to_thread(renderer.get_submission) == saved
                    if first is not None:
                        assert fixture.state.get(str(saved["js"])) == first.encode()

    asyncio.run(scenario())


@pytest.mark.parametrize("suffix", [".js", "/white/front_left.png", "/embeddings.npz", ".json"])
def test_failed_final_save_is_retryable_and_not_reported_as_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    original = fixture.state.put
                    original_save = fixture.state.save
                    prompt = hashlib.sha256(fixture.image).hexdigest()

                    def fail_index(key: str, data: bytes, expected: str | None) -> Snapshot:
                        if (
                            suffix == ".json"
                            and key.endswith("/submitted.json")
                            and prompt in GenerationsAdapter.validate_json(data)
                        ):
                            raise OSError("Storage unavailable")
                        return original_save(key, data, expected)

                    def fail(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
                        if key.endswith(suffix):
                            raise OSError("Storage unavailable")
                        original(key, data, content_type)

                    monkeypatch.setattr(fixture.state, "put", fail)
                    monkeypatch.setattr(fixture.state, "save", fail_index)
                    with pytest.raises(ArtifactUnavailable) as error:
                        await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert error.value.status == 503
                    with pytest.raises(ArtifactUnavailable) as missing:
                        await asyncio.to_thread(renderer.get_submission)
                    assert missing.value.status == 404
                    monkeypatch.setattr(fixture.state, "put", original)
                    monkeypatch.setattr(fixture.state, "save", original_save)
                    result = await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert fixture.state.get(str(result["js"])) == SOURCE.encode()
                    assert fixture.state.get(f"{result['views']}/embeddings.npz") == b"fixture embeddings"

    asyncio.run(scenario())


def test_embedding_failure_is_reported_and_the_miner_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                embeddings = AsyncMock(side_effect=OSError("Embedding calculation failed"))
                monkeypatch.setattr("pod_gateway.judging.build_embeddings_npz", embeddings)
                with client(fixture, witness.renderer_transport()) as renderer:
                    with pytest.raises(ArtifactUnavailable) as error:
                        await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert error.value.status == 503
                    embeddings.side_effect = None
                    embeddings.return_value = b"recovered embeddings"
                    result = await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert result["failure_reason"] is None
                    assert fixture.state.get(f"{result['views']}/embeddings.npz") == b"recovered embeddings"

    asyncio.run(scenario())


def test_render_failure_is_returned_for_the_miner_to_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture, fail_first_render=True)
                with client(fixture, witness.renderer_transport()) as renderer:
                    with pytest.raises(ArtifactUnavailable) as failed:
                        await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert failed.value.status == 503
                    result = await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert result["failure_reason"] is None and result["views"]

    asyncio.run(scenario())


def test_concurrent_result_index_updates_preserve_each_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    state = MemoryState()
    scope = Scope("test", "test-0", "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty")
    prompts = ("a" * 64, "b" * 64)

    def repository() -> ArtifactRepository:
        return ArtifactRepository(
            state, scope, index=state, seed=42, submission_commit=COMMIT, prompt_hashes=frozenset(prompts)
        )

    first, second = repository(), repository()
    barrier, loads = threading.Barrier(2), count()
    original = state.load

    def load(key: str) -> Snapshot | None:
        saved = original(key)
        if key == first.index_key and next(loads) < 2:
            barrier.wait(timeout=5)
        return saved

    monkeypatch.setattr(state, "load", load)
    results = [
        GenerationResult(js=store.prefix(prompt) + ".js", views=store.prefix(prompt), size=10)
        for store, prompt in zip((first, second), prompts, strict=True)
    ]

    async def publish() -> None:
        await asyncio.gather(
            asyncio.to_thread(first.update_result, prompts[0], results[0]),
            asyncio.to_thread(second.update_result, prompts[1], results[1]),
        )

    asyncio.run(publish())
    assert first.result(prompts[0]) == results[0]
    assert second.result(prompts[1]) == results[1]


def test_only_one_final_save_runs_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        started, release = asyncio.Event(), asyncio.Event()

        async def embeddings(*args: object, **kwargs: object) -> bytes:
            started.set()
            await release.wait()
            return b"fixture embeddings"

        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                monkeypatch.setattr("pod_gateway.judging.build_embeddings_npz", embeddings)
                with (
                    client(fixture, witness.renderer_transport()) as first,
                    client(fixture, witness.renderer_transport()) as second,
                ):
                    pending = asyncio.create_task(asyncio.to_thread(first.save_final, SOURCE))
                    try:
                        await asyncio.wait_for(started.wait(), 5)
                        with pytest.raises(ArtifactUnavailable) as busy:
                            await asyncio.to_thread(second.save_final, SOURCE + "\n// other")
                        assert busy.value.status == 429
                    finally:
                        release.set()
                        result = await pending
                    assert fixture.state.get(str(result["js"])) == SOURCE.encode()

    asyncio.run(scenario())


def test_new_final_saves_require_an_open_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                fixture.services[-1].round_end = 0
                with client(fixture, witness.renderer_transport()) as renderer:
                    with pytest.raises(ArtifactUnavailable) as closed:
                        await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert closed.value.status == 403

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["seed", "prompt_sha256"])
def test_final_saving_requires_the_admitted_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    renderer.generation[field] = {"seed": 43, "prompt_sha256": "d" * 64}[field]
                    with pytest.raises(ArtifactUnavailable) as rejected:
                        await asyncio.to_thread(renderer.save_final, None)
                    assert rejected.value.status in (400, 403, 409)

    asyncio.run(scenario())


def test_final_save_rejects_prompt_bytes_from_another_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                with client(fixture, witness.renderer_transport()) as renderer:
                    renderer.prompt_image = base64.b64encode(b"another prompt").decode()
                    with pytest.raises(ArtifactUnavailable) as rejected:
                        await asyncio.to_thread(renderer.save_final, SOURCE)
                    assert rejected.value.status == 400

    asyncio.run(scenario())


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
                renderer_id=IMAGE,
                prompt_sha256=hashlib.sha256(b"prompt").hexdigest(),
                prompt_image=b"prompt",
                seed=42,
            ) as renderer,
            pytest.raises(ValueError, match="must be private"),
        ):
            run_and_save(generate, renderer, tmp_path / "final.json")
        assert not generated
    finally:
        tmp_path.chmod(0o700)


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
        settings = BatchSettings(tmp_path / "batch", 42, "fixture")
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture)
                original = witness.renderer_transport
                lost = False

                def transport() -> httpx.BaseTransport:
                    inner = original()

                    def deliver(request: httpx.Request) -> httpx.Response:
                        nonlocal lost
                        response = inner.handle_request(request)
                        if not lost and request.url.path == "/v1/submissions" and response.is_success:
                            lost = True
                            response.close()
                            raise httpx.ReadError("Lost final-save acknowledgement", request=request)
                        return response

                    return httpx.MockTransport(deliver)

                monkeypatch.setattr(witness, "renderer_transport", transport)
                with client(fixture, original()) as renderer:
                    result = await asyncio.to_thread(
                        run_batch, prompts, settings, witness, renderer, progress=lambda _: None
                    )
                    assert result == {"chair": "pending"} and lost
                    old = fixture.provider.list()[0]
                    fixture.selection.update(old, active=False)
                    fixture.provider.delete(old.id)
                    witness._gateway._invalidate_for("pod_not_selected")
                    await asyncio.to_thread(witness._gateway.ensure_pod)
                    await enable(fixture)
                    result = await asyncio.to_thread(
                        run_batch, prompts, settings, witness, renderer, progress=lambda _: None
                    )
                    assert result == {"chair": "selected"}
                assert fixture.paid == 1 and len(generated) == 1
                assert (settings.output / "chair/selected.js").read_text() == generated[0]
                store = fixture.services[-1].artifacts
                assert store is not None
                assert fixture.state.get(store.prefix(prompts[0].sha256) + ".js") == generated[0].encode()

    asyncio.run(scenario())
