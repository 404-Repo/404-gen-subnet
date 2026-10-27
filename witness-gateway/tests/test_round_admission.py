"""Authenticated Pods enforce refreshed policies at execution boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import openai
import pytest
from subnet_common.competition.generations import GenerationResult
from test_artifacts import SOURCE, client, enable
from test_rounds import Competition, policy, publication
from test_service import Harness, call_key
from witness_client import WitnessError

from pod_gateway.auth import AuthRejected
from pod_gateway.models import PodSpec
from pod_gateway.rounds import PromptImage, RoundInputs, RoundPolicy


def freeze(fixture: Harness, *, images: tuple[bytes, ...] | None = None) -> RoundInputs:
    runtime = fixture.runtimes[-1]
    inputs = RoundInputs(
        repository="404-repo/404-active-competition", branch="persistent/witness", revision="a" * 40,
        round=runtime.scope.round, number=0, seed=42, generation_deadline_block=None, storage_prefix="",
        prompts=tuple(PromptImage(id=f"official-{i}", source_url=f"https://images.test/{i}.png",
                                 sha256=hashlib.sha256(image).hexdigest(),
                                 image_key=f"competition-inputs/v1/{hashlib.sha256(image).hexdigest()}")
                      for i, image in enumerate(images or (fixture.image,))),
    )
    runtime.round_inputs = inputs
    runtime.round_end = fixture.services[-1].round_end = None
    runtime.install_policy(policy(inputs).model_dump(), runtime.pod_id, runtime.start_id)
    return inputs


def install(fixture: Harness, value: RoundPolicy | None) -> None:
    runtime = fixture.runtimes[-1]
    runtime.install_policy(None if value is None else value.model_dump(), runtime.pod_id, runtime.start_id)


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("closure", ["paused", "collecting", "stale", "mismatched", "disconnected"])
def test_direct_pod_denies_new_generation_but_keeps_saved_results_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closure: str,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                inputs = freeze(fixture)
                rendered = await enable(fixture, prompt_ids=inputs.prompt_ids)
                saved = await asyncio.to_thread(witness.submit, fixture.image, None)
                if closure == "stale":
                    closed = policy(inputs, expires=time.time() - 1)
                elif closure == "mismatched":
                    closed = policy(inputs).model_copy(update={"round": "test-1"})
                else:
                    closed = policy(inputs, stage=closure)
                install(fixture, None if closure == "disconnected" else closed)
                with pytest.raises(WitnessError):
                    await asyncio.to_thread(witness._gateway.start, call_key())
                assert await asyncio.to_thread(witness.get_submitted) == {"official-0": saved}
                assert await asyncio.to_thread(witness.submit, fixture.image, None) == saved
                assert fixture.paid == 0 and rendered == []
                runtime = fixture.runtimes[-1]
                assert not runtime.draining and runtime.selected()
                install(fixture, policy(inputs))
                assert (await asyncio.to_thread(witness._gateway.start, call_key())).call.phase == "assigned"
    asyncio.run(scenario())


@pytest.mark.parametrize("source", [None, SOURCE])
def test_final_save_rechecks_policy_when_it_leaves_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str | None,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        second = b"another selected prompt"
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                inputs = freeze(fixture, images=(fixture.image, second))
                rendered = await enable(fixture, prompt_ids=inputs.prompt_ids)
                saved = await asyncio.to_thread(witness.submit, fixture.image, None)
                service = fixture.services[-1]
                await service._save_lock.acquire()
                pending = asyncio.create_task(asyncio.to_thread(witness.submit, second, source))
                await until(lambda: service._pending_saves == 1)
                install(fixture, policy(inputs, stage="paused"))
                service._save_lock.release()
                with pytest.raises(WitnessError):
                    await pending
                assert await asyncio.to_thread(witness.get_submitted) == {"official-0": saved}
                assert rendered == [] and fixture.paid == 0
    asyncio.run(scenario())


def test_an_admitted_final_save_finishes_after_pause_without_overwriting_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                inputs = freeze(fixture)
                rendered = await enable(fixture, prompt_ids=inputs.prompt_ids)
                saver = fixture.services[-1].result_saver
                assert saver is not None
                admitted, finish = asyncio.Event(), asyncio.Event()
                original = saver.save

                async def save(prompt: str, source: str | None, image: bytes | None) -> GenerationResult:
                    admitted.set()
                    await finish.wait()
                    return await original(prompt, source, image)

                monkeypatch.setattr(saver, "save", save)
                pending = asyncio.create_task(asyncio.to_thread(witness.submit, fixture.image, SOURCE))
                await asyncio.wait_for(admitted.wait(), 5)
                install(fixture, policy(inputs, stage="paused"))
                finish.set()
                saved = await pending
                assert await asyncio.to_thread(witness.submit, fixture.image, SOURCE) == saved
                with pytest.raises(WitnessError):
                    await asyncio.to_thread(witness.submit, fixture.image, SOURCE + "\n// different")
                assert await asyncio.to_thread(witness.get_submitted) == {"official-0": saved}
                assert rendered == [SOURCE] and fixture.paid == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["before_release", "after_reservation"])
def test_closure_is_checked_immediately_before_releasing_a_provider_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                inputs = freeze(fixture)
                runtime = fixture.runtimes[-1]
                if boundary == "before_release":
                    original_relay = fixture.relay

                    def close_then_relay(*args: Any, **kwargs: Any) -> dict[str, object]:
                        install(fixture, policy(inputs, stage="paused"))
                        return original_relay(*args, **kwargs)

                    monkeypatch.setattr("pod.relay", close_then_relay)
                else:
                    original_reserve = runtime.budget.reserve

                    def close_after_reservation(*args: Any, **kwargs: Any) -> Any:
                        value = original_reserve(*args, **kwargs)
                        install(fixture, policy(inputs, stage="paused"))
                        return value

                    monkeypatch.setattr(runtime.budget, "reserve", close_after_reservation)
                with pytest.raises(openai.APIConnectionError):
                    await asyncio.to_thread(witness.create, model="fixture", input="image")
                assert fixture.paid == 0
                assert any(key.endswith("/status.json") for key in fixture.state.objects)
    asyncio.run(scenario())


def test_assigned_miner_with_no_legacy_commit_can_submit_and_recover_saved_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                inputs = freeze(fixture)
                await enable(fixture, prompt_ids=inputs.prompt_ids)
                store = fixture.services[-1].artifacts
                assert store is not None
                store.submission_commit = None
                with client(fixture, witness.renderer_transport()) as legacy:
                    assert await asyncio.to_thread(legacy.load_context) == {"submission_commit": None}
                    assert await asyncio.to_thread(legacy.save_final, None) == await asyncio.to_thread(
                        witness.submit, fixture.image, None,
                    )
                install(fixture, policy(inputs, stage="collecting"))
                assert (await asyncio.to_thread(witness.get_submitted))["official-0"]["failure_reason"] == "generation_failed"
    asyncio.run(scenario())


def test_shared_controller_rolls_new_allocations_forward_and_fences_old_pods_without_retargeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        competition = Competition(publication(prefix=""), state=fixture.state)
        async with fixture.running():
            fixture.gateway.rounds = competition.controller
            base = fixture.allocator.spec
            fixture.gateway.spec_for_round = lambda inputs: replace(base, env={
                "WITNESS_ROUND_INPUTS": inputs.model_dump_json(),
            })
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                old = fixture.runtimes[-1]
                frozen = old.round_inputs
                assert frozen == competition.controller.require()
                old.require_generation()
                fixture.image = competition.images["chair"]
                await enable(fixture, prompt_ids=frozen.prompt_ids)
                saved = await asyncio.to_thread(witness.submit, fixture.image, None)
                competition.source.publish(publication(number=1, seed=91, prompt="lamp", stage="open", prefix=""))
                await competition.controller.refresh()
                await fixture.gateway.round_changed()
                await until(lambda: old.round_policy is not None and old.round_policy.stage == "open")
                with pytest.raises(AuthRejected):
                    old.require_generation()
                assert not old.draining and await asyncio.to_thread(witness.get_submitted) == {"chair": saved}
                competition.source.publish(publication(number=1, seed=91, prompt="lamp", prefix=""))
                await competition.controller.refresh()
                await fixture.gateway.round_changed()
                assert fixture.gateway.round == "test-1" and old.round_inputs == frozen
                with fixture.client("next-round") as current:
                    await asyncio.to_thread(current._gateway.ensure_pod)
                    newer = fixture.runtimes[-1]
                    assert newer is not old and newer.round_inputs is not None
                    assert newer.round_inputs.seed == 91 and list(newer.round_inputs.prompt_ids.values()) == ["lamp"]
                    newer.require_generation()
                    assert old.records is not None and fixture.paid == 0
    asyncio.run(scenario())


def test_queued_gateway_allocation_cannot_create_after_the_round_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_lifecycle import FIRST

    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        competition = Competition(publication(prefix=""), state=fixture.state)
        async with fixture.running():
            fixture.gateway.rounds = competition.controller
            fixture.gateway.spec_for_round = lambda inputs: PodSpec(
                fixture.allocator.spec.image, env={"WITNESS_ROUND_INPUTS": inputs.model_dump_json()},
            )
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            fixture.gateway.queue.enqueue(fixture.scope, FIRST, None)
            competition.source.publish(publication(stage="paused", prefix=""))
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            pending = fixture.gateway.queue.pending(fixture.scope)
            assert pending is not None
            with pytest.raises(AuthRejected, match="round_closed"):
                fixture.gateway._advance(fixture.scope, pending)
            assert fixture.provider.creates == 0
            assert fixture.gateway.queue.pending(fixture.scope) is not None
    asyncio.run(scenario())


def test_namespaced_recordings_recover_after_closure_without_a_second_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        competition = Competition(state=fixture.state)
        async with fixture.running():
            fixture.gateway.rounds = competition.controller
            base = fixture.allocator.spec
            fixture.gateway.spec_for_round = lambda inputs: replace(
                base, env={"WITNESS_ROUND_INPUTS": inputs.model_dump_json()},
            )
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            with fixture.client() as witness:
                answer = await asyncio.to_thread(witness.create, model="fixture", input="image")
            competition.source.publish(publication(stage="collecting"))
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            runtime = fixture.runtimes[-1]
            await until(lambda: runtime.round_policy is not None and runtime.round_policy.stage == "collecting")
            fixture.provider.delete(runtime.pod_id)
            await until(lambda: runtime.pod_id not in fixture.gateway._pods)
            with fixture.client("recording-recovery") as recovered:
                assert (await asyncio.to_thread(recovered.create, model="fixture", input="image")).output_text == answer.output_text
            assert fixture.paid == 1 and fixture.provider.creates == 2
            assert any(key.startswith("isolated/rounds/") and key.endswith("/manifest.json")
                       for key in fixture.state.objects)
            assert not any(key.startswith("rounds/") for key in fixture.state.objects)
    asyncio.run(scenario())


def test_saved_results_remain_available_after_the_pod_is_lost_in_a_closed_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        files = publication(prefix="")
        files["rounds/0/prompts.txt"] += "\nhttps://images.test/lamp.png"
        competition = Competition(files, state=fixture.state)
        fixture.image = competition.images["chair"]
        async with fixture.running():
            fixture.gateway.rounds = competition.controller
            base = fixture.allocator.spec
            fixture.gateway.spec_for_round = lambda inputs: replace(
                base, env={"WITNESS_ROUND_INPUTS": inputs.model_dump_json()},
            )
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            inputs = competition.controller.require()
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                await enable(fixture, prompt_ids=inputs.prompt_ids)
                saved = await asyncio.to_thread(witness.submit, fixture.image, None)
            closed = {**files, "state.json": '{"current_round":0,"stage":"paused"}'}
            competition.source.publish(closed)
            competition.controller.inputs = None
            competition.controller._resolved.clear()
            fixture.gateway.round = "unconfigured"
            await competition.controller.refresh()
            await fixture.gateway.round_changed()
            old = fixture.runtimes[-1]
            fixture.provider.delete(old.pod_id)
            await until(lambda: old.pod_id not in fixture.gateway._pods)
            with fixture.client("saved-recovery") as recovered:
                await asyncio.to_thread(recovered._gateway.ensure_pod)
                await enable(fixture, prompt_ids=inputs.prompt_ids)
                assert await asyncio.to_thread(recovered.get_submitted) == {"chair": saved}
                assert await asyncio.to_thread(recovered.submit, fixture.image, None) == saved
                with pytest.raises(WitnessError, match="round_closed"):
                    await asyncio.to_thread(recovered.submit, competition.images["lamp"], None)
                with pytest.raises(WitnessError, match="round_closed"):
                    await asyncio.to_thread(recovered._gateway.start, call_key())
            assert fixture.paid == 0 and fixture.provider.creates == 2
    asyncio.run(scenario())


def test_disabled_final_storage_returns_an_error_while_provider_recordings_remain_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = Harness(tmp_path, monkeypatch)
        async with fixture.running():
            with fixture.client() as witness:
                await asyncio.to_thread(witness._gateway.ensure_pod)
                inputs = freeze(fixture).model_copy(update={"save_submissions": False})
                fixture.runtimes[-1].round_inputs = inputs
                install(fixture, policy(inputs))
                with pytest.raises(WitnessError, match="artifact_storage_disabled"):
                    await asyncio.to_thread(witness.submit, fixture.image, None)
                answer = await asyncio.to_thread(witness.create, model="fixture", input="image")
                assert answer.output_text and fixture.paid == 1
                assert any(key.endswith("/manifest.json") for key in fixture.state.objects)
    asyncio.run(scenario())


def test_gateway_restart_retains_a_pending_pod_until_its_frozen_specification_is_resolved() -> None:
    from support import MemoryState, Provider
    from test_gateway_limits import KEYS, FIRST, coordinator

    async def scenario() -> None:
        state, provider = MemoryState(), Provider()
        competition = Competition(publication(number=1), state=state)
        await competition.controller.refresh()
        frozen = competition.controller.require()
        async with coordinator(provider, state, [1000.0]) as gateway:
            base = gateway.allocator.spec
            spec = replace(base, env={"WITNESS_ROUND_INPUTS": frozen.model_dump_json()})
            pending = provider.create(spec.body(gateway.scope(KEYS[0].ss58_address), FIRST))
            gateway._publish(provider.list())
            competition.controller.inputs = None
            competition.controller.policy = None
            gateway.rounds = competition.controller
            gateway.spec_for_round = lambda inputs: replace(base, env={"WITNESS_ROUND_INPUTS": inputs.model_dump_json()})
            await gateway._step()
            assert provider.pods[pending.id].lifecycle == "pending" and provider.creates == 1
            await competition.controller.refresh()
            await gateway.round_changed()
            await gateway._step()
            assert provider.pods[pending.id].lifecycle == "active" and provider.creates == 1
    asyncio.run(scenario())
