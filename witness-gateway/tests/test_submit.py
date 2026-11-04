"""Live SDK submission through local authenticated Pods and mocked judging."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from bittensor_wallet import Keypair
from renderer_client import ArtifactUnavailable, RenderOptions, RenderRejected, RenderUnavailable
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from test_artifacts import PNG, SOURCE, client, enable
from test_service import Harness
from witness_client import AsyncWitnessClient, WitnessClient, WitnessError
from witness_client.async_tunnel import AsyncWebSocketTunnel

from pod_gateway.artifacts import ArtifactRepository, SubmissionContext
from pod_gateway.models import Scope

Mode = Literal["sync", "async"]


@asynccontextmanager
async def live_client(fixture: Harness, mode: Mode) -> AsyncIterator[WitnessClient | AsyncWitnessClient]:
    if mode == "sync":
        with fixture.client() as witness:
            await asyncio.to_thread(witness._gateway.ensure_pod)
            yield witness
    else:
        witness_async = AsyncWitnessClient(
            {
                "witness_url": fixture.origin,
                "proof_parameters": "unused",
                "state_directory": str(fixture.directory / "async-client"),
            },
            hotkey=fixture.key,
            openai_api_key="fixture-" + "a" * 80,
        )
        try:
            await witness_async._gateway.ensure_pod()
            yield witness_async
        finally:
            await witness_async.aclose()


async def submit(witness: WitnessClient | AsyncWitnessClient, image: bytes, source: str | None) -> dict[str, object]:
    if isinstance(witness, AsyncWitnessClient):
        return await witness.submit(image, source)
    return await asyncio.to_thread(witness.submit, image, source)


async def get_submitted(witness: WitnessClient | AsyncWitnessClient) -> dict[str, dict[str, object]]:
    if isinstance(witness, AsyncWitnessClient):
        return await witness.get_submitted()
    return await asyncio.to_thread(witness.get_submitted)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_get_submitted_resumes_only_this_miners_round_after_client_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            async with live_client(fixture, mode) as witness:
                failed_image = b"failed frozen prompt"
                rendered = await enable(fixture, prompt_ids={
                    hashlib.sha256(fixture.image).hexdigest(): "official-chair",
                    hashlib.sha256(failed_image).hexdigest(): "official-empty",
                })
                store = fixture.services[-1].artifacts
                assert store is not None
                assert await get_submitted(witness) == {}
                expected = {
                    "official-chair": await submit(witness, fixture.image, SOURCE),
                    "official-empty": await submit(witness, failed_image, None),
                }
                assert str(expected["official-chair"]["js"]).endswith("/official-chair.js")
                assert GenerationsAdapter.validate_json(fixture.state.get(store.index_key) or b"{}") == {
                    prompt: GenerationResult.model_validate(result) for prompt, result in expected.items()
                }
                for scope in (
                    Scope(store.scope.deployment, "another-round", store.scope.hotkey),
                    Scope(store.scope.deployment, store.scope.round, Keypair.create_from_uri("//Bob").ss58_address),
                ):
                    foreign = ArtifactRepository(
                        fixture.state, scope, index=fixture.state, seed=store.seed,
                        submission_commit=store.submission_commit, prompt_ids={"f" * 64: "f" * 64},
                    )
                    foreign.update_result("f" * 64, GenerationResult(failure_reason="generation_failed"))
            fixture.services[-1].round_end = 0
            async with live_client(fixture, mode) as resumed:
                assert await get_submitted(resumed) == expected
                assert rendered == [SOURCE] and fixture.paid == 0 and fixture.provider.creates == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("source", [SOURCE, None])
def test_submit_uses_round_seed_and_remains_compatible_with_existing_artifact_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    source: str | None,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.image = PNG
        async with fixture.running(), live_client(fixture, mode) as witness:
            rendered = await enable(fixture)
            # Use a seed the new SDK cannot infer from its constructor or image.
            store = fixture.services[-1].artifacts
            assert store is not None
            store.seed = 987654321
            owned = witness._gateway._direct
            assert owned is not None
            result = await submit(witness, fixture.image, source)
            assert witness._gateway._direct is owned and not owned.http.is_closed
            assert await submit(witness, fixture.image, source) == result
            if source is None:
                assert result == {"js": None, "views": None, "failure_reason": "generation_failed", "size": 0}
            else:
                assert fixture.state.get(str(result["js"])) == source.encode()
                for name in (
                    "white/front_left.png",
                    "white/back.png",
                    "gray/front_above.png",
                    "grid.png",
                    "embeddings.npz",
                ):
                    assert fixture.state.get(f"{result['views']}/{name}") is not None
                assert fixture.state.get(f"{result['views']}/embeddings.npz") == b"fixture embeddings"
            if isinstance(witness, WitnessClient):
                with client(fixture, witness.renderer_transport()) as legacy:
                    legacy.generation["seed"] = store.seed
                    assert await asyncio.to_thread(legacy.save_final, source) == result
                    legacy.generation["seed"] = store.seed + 1
                    with pytest.raises(ArtifactUnavailable) as mismatch:
                        await asyncio.to_thread(legacy.save_final, source)
                    assert mismatch.value.status == 400
            assert rendered == ([] if source is None else [source])
            assert fixture.paid == 0 and fixture.provider.creates == 1
        assert owned.http.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("source", [SOURCE, None])
def test_saved_result_resubmission_after_lost_reply_does_not_regenerate_or_render_previews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    source: str | None,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running(), live_client(fixture, mode) as witness:
            rendered = await enable(fixture, prompt_ids={hashlib.sha256(fixture.image).hexdigest(): "official-choice"})
            attempts: list[str] = []

            def delivered(request: httpx.Request, response: httpx.Response) -> httpx.Response:
                assert request.url.path == "/v1/submissions"
                if request.method == "POST":
                    attempts.append(request.url.path)
                    if len(attempts) == 1:
                        raise httpx.ReadError("Final reply lost after durable saving", request=request)
                return response

            if isinstance(witness, AsyncWitnessClient):
                async_original = witness._gateway.send

                async def delivery(request: httpx.Request) -> httpx.Response:
                    response = await async_original(request)
                    try:
                        return delivered(request, response)
                    finally:
                        await response.aclose()

                monkeypatch.setattr(witness._gateway, "send", delivery)
            else:
                sync_original = witness._gateway.send

                def sync_delivery(request: httpx.Request) -> httpx.Response:
                    response = sync_original(request)
                    try:
                        return delivered(request, response)
                    finally:
                        response.close()

                monkeypatch.setattr(witness._gateway, "send", sync_delivery)
            with pytest.raises(WitnessError) as lost:
                await submit(witness, fixture.image, source)
            assert lost.value.code == "submission_unresolved" and len(attempts) == 1
            fixture.services[-1].round_end = 0
            retrieved = await get_submitted(witness)
            saved = await submit(witness, fixture.image, source)
            assert retrieved == {"official-choice": saved}
            assert len(attempts) == 2 and saved["failure_reason"] == ("generation_failed" if source is None else None)
            assert rendered == ([] if source is None else [source])
            assert fixture.paid == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("first,second", [(SOURCE, SOURCE + "\n// conflicting"), (SOURCE, None), (None, SOURCE)])
def test_submit_conflicts_keep_the_existing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    first: str | None,
    second: str | None,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running(), live_client(fixture, mode) as witness:
            rendered = await enable(fixture)
            saved = await submit(witness, fixture.image, first)
            with pytest.raises(WitnessError) as conflict:
                await submit(witness, fixture.image, second)
            assert conflict.value.status_code == 409 and conflict.value.code == "RecordingConflict"
            assert await submit(witness, fixture.image, first) == saved
            assert rendered == ([] if first is None else [first]) and fixture.paid == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["render", "embeddings", "closed", "prompt", "rejected", "unselected", "auth"])
def test_submit_propagates_admission_and_judging_failures_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running(), live_client(fixture, "async") as witness:
            rendered = await enable(fixture, fail_first_render=failure == "render")
            image, source, expected = fixture.image, SOURCE, 503
            if failure == "embeddings":
                monkeypatch.setattr(
                    "pod_gateway.results.build_embeddings_npz", AsyncMock(side_effect=OSError("Unavailable"))
                )
            elif failure == "closed":
                fixture.services[-1].round_end = 0
                expected = 403
            elif failure == "prompt":
                image, expected = b"different frozen image", 403
            elif failure == "rejected":
                source, expected = "invalid", 422
            elif failure == "unselected":
                fixture.selection.update(fixture.provider.list()[0], active=False)
                expected = 409
            elif failure == "auth":
                assert witness._gateway._direct is not None
                witness._gateway._direct.headers["Authorization"] = "Bearer invalid"
                expected = 401
            with pytest.raises(WitnessError) as failed:
                await submit(witness, image, source)
            assert failed.value.status_code == expected
            if failure in ("closed", "prompt", "unselected", "auth"):
                assert rendered == []
            if failure in ("render", "embeddings"):
                await enable(fixture)
                assert (await submit(witness, image, source))["failure_reason"] is None
            assert fixture.paid == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["sync", "async", "artifact"])
def test_slow_final_saves_complete_while_another_save_is_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    # Scale finite client timeouts from seconds to milliseconds; use real local TLS I/O.
    original_sync = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request

    def scale_timeout(request: httpx.Request) -> None:
        if request.method == "POST" and request.url.path == "/v1/submissions":
            request.extensions["timeout"] = {
                name: None if seconds is None else seconds / 1000
                for name, seconds in request.extensions["timeout"].items()
            }

    def handle_sync(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        scale_timeout(request)
        return original_sync(transport, request)

    async def handle_async(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        scale_timeout(request)
        return await original_async(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        started, release, arrived = asyncio.Event(), asyncio.Event(), asyncio.Event()
        images = [fixture.image, b"another frozen prompt"]
        prompts = [hashlib.sha256(image).hexdigest() for image in images]
        sources = [SOURCE, SOURCE + "\n// another"]
        async with fixture.running(), live_client(fixture, "async" if kind == "async" else "sync") as witness:
            rendered = await enable(fixture, prompt_ids={prompt: prompt for prompt in prompts})
            store = fixture.services[-1].artifacts
            assert store is not None
            original_require = store.require

            def require(generation: SubmissionContext) -> None:
                original_require(generation)
                if generation.prompt_sha256 == prompts[1]:
                    arrived.set()

            async def embeddings(image: bytes, *args: object, **kwargs: object) -> bytes:
                if image == images[0]:
                    started.set()
                    await release.wait()
                return b"retained embeddings"

            monkeypatch.setattr(store, "require", require)
            monkeypatch.setattr("pod_gateway.results.build_embeddings_npz", embeddings)
            with ExitStack() as stack:
                legacy = []
                if kind == "artifact":
                    assert isinstance(witness, WitnessClient)
                    legacy = [stack.enter_context(client(
                        fixture, witness.renderer_transport(), prompt_image=image,
                    )) for image in images]

                async def save(index: int) -> dict[str, object]:
                    if legacy:
                        return await asyncio.to_thread(legacy[index].save_final, sources[index])
                    return await submit(witness, images[index], sources[index])

                pending = [asyncio.create_task(save(0))]
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    pending.append(asyncio.create_task(save(1)))
                    await asyncio.wait_for(arrived.wait(), 5)
                    await asyncio.sleep(0.3)
                finally:
                    release.set()
                    results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
                for result, source in zip(results, sources, strict=True):
                    assert isinstance(result, dict), result
                    assert fixture.state.get(str(result["js"])) == source.encode()
                    assert result["failure_reason"] is None
                assert rendered == sources and fixture.paid == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("case", ["pool_timeout", "stale_auth"])
def test_concurrent_final_saves_reach_the_pod_with_fresh_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode, case: str,
) -> None:
    original_sync = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request

    def scale_pool_timeout(request: httpx.Request) -> None:
        if case == "pool_timeout" and request.method == "POST" and request.url.path == "/v1/submissions":
            request.extensions["timeout"]["pool"] /= 750

    def handle_sync(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        scale_pool_timeout(request)
        return original_sync(transport, request)

    async def handle_async(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        scale_pool_timeout(request)
        return await original_async(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)

    async def scenario() -> None:
        if mode == "sync":
            # Client calls and Pod storage share this test loop's executor.
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=128))
        fixture = Harness(tmp_path, monkeypatch)
        images = [f"prompt-{i}".encode() for i in range(101)]
        prompts = [hashlib.sha256(image).hexdigest() for image in images]
        started, release, arrived = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async with fixture.running(), live_client(fixture, mode) as witness:
            await enable(fixture, prompt_ids={prompt: prompt for prompt in prompts})
            service = fixture.services[-1]
            store, saver = service.artifacts, service.result_saver
            assert store is not None and saver is not None
            original_save, original_require = saver.save, store.require
            arrivals: set[str] = set()

            def require(generation: SubmissionContext) -> None:
                original_require(generation)
                arrivals.add(generation.prompt_sha256)
                if len(arrivals) == 100:
                    arrived.set()

            async def save(prompt: str, source: str | None, image: bytes | None) -> GenerationResult:
                if prompt == prompts[0]:
                    started.set()
                    await release.wait()
                return await original_save(prompt, source, image)

            monkeypatch.setattr(store, "require", require)
            monkeypatch.setattr(saver, "save", save)
            pending = [asyncio.create_task(submit(witness, images[0], None))]
            try:
                await asyncio.wait_for(started.wait(), 5)
                pending.extend(asyncio.create_task(submit(witness, image, None)) for image in images[1:])
                await asyncio.wait_for(arrived.wait(), 10)
                await asyncio.sleep(11 if case == "stale_auth" else 0.3)
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 30)
            for result in results:
                assert isinstance(result, dict), result
                assert result["failure_reason"] == "generation_failed"
            assert len(store.results()) == 101 and fixture.paid == 0

    asyncio.run(scenario())


def test_cancelled_async_submission_can_retrieve_the_result_saved_by_the_pod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        started, release = asyncio.Event(), asyncio.Event()
        async with fixture.running(), live_client(fixture, "async") as witness:
            assert isinstance(witness, AsyncWitnessClient)
            rendered = await enable(fixture)
            store = fixture.services[-1].artifacts
            assert store is not None

            async def embeddings(*args: object, **kwargs: object) -> bytes:
                started.set()
                await release.wait()
                return b"retained embeddings"

            monkeypatch.setattr("pod_gateway.results.build_embeddings_npz", embeddings)
            task = asyncio.create_task(witness.submit(fixture.image, SOURCE))
            try:
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            prompt = hashlib.sha256(fixture.image).hexdigest()
            async with asyncio.timeout(5):
                while await asyncio.to_thread(store.result, prompt) is None:
                    await asyncio.sleep(0.01)
            result = (await witness.get_submitted())[prompt]
            assert fixture.state.get(f"{result['views']}/embeddings.npz") == b"retained embeddings"
            assert rendered == [SOURCE] and fixture.paid == 0

    asyncio.run(scenario())


def model_transport(fixture: Harness) -> None:
    async def exchange(
        tunnel: AsyncWebSocketTunnel, host: str, chunks: list[tuple[bool, bytes]], **kwargs: object,
    ) -> bytes:
        body = b"".join(chunk for _, chunk in chunks).partition(b"\r\n\r\n")[2]
        await tunnel.send(len(body).to_bytes(8, "big") + body)
        await tunnel.flush()
        response = bytearray()
        while chunk := await tunnel.recv(timeout=10):
            response.extend(chunk)
        return bytes(response)

    fixture.monkeypatch.setattr(
        "witness_client.async_client.Prover", lambda _: SimpleNamespace(verifying_key_sha256=fixture.proof)
    )
    fixture.monkeypatch.setattr("witness_client.async_client.async_exchange", exchange)


async def generated_source(witness: WitnessClient | AsyncWitnessClient) -> str:
    if isinstance(witness, AsyncWitnessClient):
        return (await witness.responses.create(model="fixture", input="image", request_id=7)).output_text
    return (await asyncio.to_thread(witness.responses.create, model="fixture", input="image", request_id=7)).output_text


@pytest.mark.parametrize("failure", ["unknown", "http404", "malformed"])
def test_async_caller_retries_a_failed_request_with_the_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.failures = [failure]
        model_transport(fixture)
        async with fixture.running(), live_client(fixture, "async") as witness:
            assert isinstance(witness, AsyncWitnessClient)
            with pytest.raises(openai.APIError) as error:
                await witness.responses.create(model="fixture", input="image", request_id=7)
            if failure == "http404":
                assert isinstance(error.value, openai.NotFoundError)
            assert fixture.paid == 1
            response = await witness.responses.create(model="fixture", input="image", request_id=7)
            assert response.output_text == "answer-2" and fixture.paid == 2
            saved = await witness.responses.create(model="fixture", input="image", request_id=7)
            assert saved.model_dump() == response.model_dump() and fixture.paid == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_fresh_client_recovers_a_lost_model_result_without_local_state_or_duplicate_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            model_transport(fixture)
            async with live_client(fixture, mode) as first:
                if isinstance(first, AsyncWitnessClient):
                    async def lost_async(*args: object, **kwargs: object) -> None:
                        raise httpx.ReadError("Recorded model result reply lost")
                    monkeypatch.setattr(first._gateway, "recorded", lost_async)
                else:
                    def lost_sync(*args: object, **kwargs: object) -> None:
                        raise httpx.ReadError("Recorded model result reply lost")
                    monkeypatch.setattr(first._gateway, "recorded", lost_sync)
                with pytest.raises(openai.APIConnectionError):
                    await generated_source(first)
                assert fixture.paid == 1
            async with live_client(fixture, mode) as restarted:
                assert await generated_source(restarted) == "answer-1"
                assert fixture.paid == fixture.provider.creates == 1
            assert not (tmp_path / "client").exists() and not (tmp_path / "async-client").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_failed_submission_can_rerun_generation_from_pod_recordings_and_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.response_text = lambda body, number: SOURCE
        async with fixture.running():
            model_transport(fixture)
            async with live_client(fixture, mode) as first:
                rendered = await enable(fixture, fail_first_render=True)
                assert await get_submitted(first) == {}
                with pytest.raises(WitnessError) as failed:
                    await submit(first, fixture.image, await generated_source(first))
                assert failed.value.status_code == 503
            async with live_client(fixture, mode) as restarted:
                assert await get_submitted(restarted) == {}
                result = await submit(restarted, fixture.image, await generated_source(restarted))
                assert await get_submitted(restarted) == {hashlib.sha256(fixture.image).hexdigest(): result}
                assert fixture.paid == fixture.provider.creates == 1 and rendered == [SOURCE]
            assert not (tmp_path / "client").exists() and not (tmp_path / "async-client").exists()

    asyncio.run(scenario())


def test_fresh_async_client_joins_an_ambiguously_delivered_model_call_without_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        fixture.allow_response.clear()
        async with fixture.running():
            model_transport(fixture)
            try:
                async with live_client(fixture, "async") as first:
                    task = asyncio.create_task(generated_source(first))
                    await asyncio.wait_for(asyncio.to_thread(fixture.dispatched.wait, 5), 6)
                    assert fixture.paid == 1
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                async with live_client(fixture, "async") as restarted:
                    recovered = asyncio.create_task(generated_source(restarted))
                    await asyncio.sleep(0.1)
                    assert fixture.paid == 1 and not recovered.done()
                    fixture.allow_response.set()
                    assert await asyncio.wait_for(recovered, 5) == "answer-1"
                    assert fixture.paid == fixture.provider.creates == 1
                assert not (tmp_path / "async-client").exists()
            finally:
                fixture.allow_response.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_witness_preview_uses_its_pod_and_leaves_submission_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running(), live_client(fixture, mode) as witness:
            rendered = await enable(fixture, fail_first_render=True)
            owned = witness._gateway._direct
            assert owned is not None
            options = RenderOptions(lighting="neutral")

            async def preview(source: str) -> bytes:
                if isinstance(witness, AsyncWitnessClient):
                    return await witness.render(source, options)
                return await asyncio.to_thread(witness.render, source, options)

            with pytest.raises(RenderUnavailable):
                await preview(SOURCE)
            with pytest.raises(RenderRejected) as rejected:
                await preview("invalid")
            assert rejected.value.details == {"error": "validation_failed", "failures": []}
            assert await preview(SOURCE) == PNG
            assert witness._gateway._direct is owned and not owned.http.is_closed
            assert await get_submitted(witness) == {}
            assert rendered == [SOURCE] and fixture.paid == 0 and fixture.provider.creates == 1
        assert owned.http.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("source", [SOURCE, None])
def test_existing_hash_keyed_results_recover_under_official_ids_without_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode, source: str | None,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running(), live_client(fixture, mode) as witness:
            digest = hashlib.sha256(fixture.image).hexdigest()
            rendered = await enable(fixture, prompt_ids={digest: "official-chair"})
            store = fixture.services[-1].artifacts
            assert store is not None
            if source is None:
                previous = GenerationResult(failure_reason="generation_failed")
            else:
                root = f"rounds/{store.scope.round}/{store.scope.hotkey}/submitted/{digest}"
                previous = GenerationResult(js=root + ".js", views=root, size=len(source.encode()))
                fixture.state.put(root + ".js", source.encode())
            fixture.state.put(store.index_key, GenerationsAdapter.dump_json({digest: previous}))
            fixture.services[-1].round_end = 0
            assert await get_submitted(witness) == {"official-chair": previous.model_dump()}
            assert GenerationsAdapter.validate_json(fixture.state.get(store.index_key) or b"{}") == {
                "official-chair": previous,
            }
            assert await submit(witness, fixture.image, source) == previous.model_dump()
            assert rendered == [] and fixture.paid == 0

    asyncio.run(scenario())
