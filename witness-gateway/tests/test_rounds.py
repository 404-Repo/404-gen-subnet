"""Offline branch snapshots, admission leases and immutable generation inputs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from PIL import Image
from subnet_common.github import GitHubClient
from support import MemoryState, Provider
from test_lifecycle import Drain, FIRST, SECOND, SCOPE, SPEC
from test_settings import values as settings_values

from pod_gateway.artifacts import ArtifactConfig, ArtifactRepository
from pod_gateway.auth import AuthRejected
from pod_gateway.dependencies import gateway_spec, worker_spec
from pod_gateway.lifecycle import Allocator
from pod_gateway.revivor import Revivor
from pod_gateway.rounds import RoundController, RoundInputs, RoundPolicy
from pod_gateway.settings import Settings
from pod_gateway.state import PrefixedState

values = settings_values


def png(color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def publication(*, number: int = 0, seed: int = 42, stage: str = "miner_generation",
                deadline: int | None = None, prefix: str = "isolated/", prompt: str = "chair") -> dict[str, str]:
    return {
        "config.json": json.dumps({"name": "test", "description": "fixture", "first_evaluation_date": "2026-01-01",
                                   "last_competition_date": "2027-01-01", "generation_stage_minutes": 180,
                                   "win_margin": 0.1, "prompts_per_round": 1, "carryover_prompts": 0}),
        "state.json": json.dumps({"current_round": number, "stage": stage, "next_stage_eta": None}),
        f"rounds/{number}/schedule.json": json.dumps({"earliest_reveal_block": 1, "latest_reveal_block": 2,
                                                     "generation_deadline_block": deadline,
                                                     "witness_storage_prefix": prefix}),
        f"rounds/{number}/seed.json": json.dumps({"seed": seed}),
        f"rounds/{number}/prompts.txt": f"https://images.test/{prompt}.png",
        "prompts.txt": "https://production.test/unrelated.png",
    }


class Source(GitHubClient):
    def __init__(self, files: dict[str, str]) -> None:
        self.revisions = {"a" * 40: files}
        self.revision = "a" * 40
        self.reads: list[tuple[str, str]] = []
        self.branches: list[str] = []
        self.after_resolve: str | None = None

    async def get_ref_sha(self, ref: str) -> str:
        self.branches.append(ref)
        revision = self.revision
        if self.after_resolve is not None:
            self.revision = self.after_resolve
        return revision

    async def get_file(self, path: str, ref: str) -> str | None:
        self.reads.append((path, ref))
        return self.revisions[ref].get(path)

    def publish(self, files: dict[str, str]) -> None:
        self.revision = f"{len(self.revisions) + 10:040x}"
        self.revisions[self.revision] = files


class Competition:
    def __init__(self, files: dict[str, str] | None = None, *, branch: str = "persistent/witness",
                 state: MemoryState | None = None) -> None:
        self.source = Source(publication() if files is None else files)
        self.state = MemoryState() if state is None else state
        self.downloaded: list[str] = []
        self.images = {"chair": png(), "lamp": png("blue")}
        self.now = [time.time()]
        self.block = [10]

        async def download(url: str) -> bytes:
            self.downloaded.append(url)
            return self.images[Path(url).stem]

        async def head() -> tuple[int, float]:
            return self.block[0], self.now[0]

        self.controller = RoundController(
            git=self.source, repo="https://github.com/404-Repo/404-active-competition.git", branch=branch,
            objects=self.state, pins=self.state, download=download, head=head, clock=lambda: self.now[0], ttl=30,
        )


def policy(inputs: RoundInputs, *, stage: str = "miner_generation", expires: float | None = None) -> RoundPolicy:
    now = time.time()
    return RoundPolicy(repository=inputs.repository, branch=inputs.branch, revision=inputs.revision,
                       round=inputs.round, inputs_sha256=inputs.fingerprint, stage=stage,
                       observed_at=now, expires_at=now + 30 if expires is None else expires,
                       generation_deadline_block=inputs.generation_deadline_block, block=10)


@pytest.mark.parametrize("branch", ["main", "test", "persistent/witness"])
def test_every_branch_uses_its_selected_round_and_content_instead_of_global_prompts(branch: str) -> None:
    async def scenario() -> None:
        competition = Competition(branch=branch)
        await competition.controller.refresh()
        inputs = competition.controller.require()
        assert inputs.seed == 42 and inputs.generation_deadline_block is None
        digest = hashlib.sha256(competition.images["chair"]).hexdigest()
        assert inputs.prompt_ids == {digest: "chair"} and digest != "chair"
        assert competition.state.get(inputs.prompts[0].image_key) == competition.images["chair"]
        assert competition.source.branches == [branch]
        assert {ref for _, ref in competition.source.reads} == {"a" * 40}
        assert competition.downloaded == ["https://images.test/chair.png"]
        competition.now[0] += 25 * 86400
        await competition.controller.refresh()
        assert competition.controller.require() == inputs
    asyncio.run(scenario())


def test_branch_is_resolved_once_even_when_it_moves_during_the_read() -> None:
    async def scenario() -> None:
        competition = Competition()
        competition.source.revisions["b" * 40] = publication(number=1, seed=17, prompt="lamp")
        competition.source.after_resolve = "b" * 40
        await competition.controller.refresh()
        assert competition.controller.require().seed == 42
        assert all(ref == "a" * 40 for _, ref in competition.source.reads)
        await competition.controller.refresh()
        newer = competition.controller.require()
        assert newer.round == "test-1" and newer.seed == 17 and list(newer.prompt_ids.values()) == ["lamp"]
    asyncio.run(scenario())


@pytest.mark.parametrize("failed_read", ["ref", "state", "config", "head"])
def test_refresh_errors_keep_permission_only_until_its_original_expiry(
    monkeypatch: pytest.MonkeyPatch, failed_read: str,
) -> None:
    async def scenario() -> None:
        competition = Competition(publication(deadline=100))
        await competition.controller.refresh()
        frozen = competition.controller.require()
        lease = competition.controller.policy
        assert lease is not None
        original = competition.source.get_file

        async def unavailable(*args: object) -> str:
            raise httpx.ReadTimeout("Competition refresh unavailable")

        async def get_file(path: str, ref: str) -> str | None:
            if path == {"state": "state.json", "config": "config.json"}[failed_read]:
                raise httpx.ReadTimeout("Competition refresh unavailable")
            return await original(path, ref)

        with monkeypatch.context() as patch:
            if failed_read == "ref":
                patch.setattr(competition.source, "get_ref_sha", unavailable)
            elif failed_read == "head":
                patch.setattr(competition.controller, "head", unavailable)
            else:
                patch.setattr(competition.source, "get_file", get_file)
            for now in (lease.expires_at - 2, lease.expires_at - 1, lease.expires_at):
                competition.now[0] = now
                with pytest.raises(httpx.ReadTimeout):
                    await competition.controller.refresh()
                if now < lease.expires_at:
                    assert competition.controller.require() == frozen
                else:
                    with pytest.raises(AuthRejected, match="round_policy_stale"):
                        competition.controller.require()
        await competition.controller.refresh()
        assert competition.controller.require() == frozen
    asyncio.run(scenario())


@pytest.mark.parametrize("failed_read", ["state.json", "config.json"])
def test_new_revision_revokes_permission_even_when_its_metadata_read_fails(
    monkeypatch: pytest.MonkeyPatch, failed_read: str,
) -> None:
    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        competition.source.publish(publication(number=1, prompt="lamp"))
        original = competition.source.get_file

        async def get_file(path: str, ref: str) -> str | None:
            if path == failed_read:
                raise httpx.ReadTimeout("Competition refresh unavailable")
            return await original(path, ref)

        with monkeypatch.context() as patch:
            patch.setattr(competition.source, "get_file", get_file)
            with pytest.raises(httpx.ReadTimeout):
                await competition.controller.refresh()
        with pytest.raises(AuthRejected):
            competition.controller.require()
        await competition.controller.refresh()
        assert competition.controller.require().round == "test-1"
    asyncio.run(scenario())


def test_new_publication_revokes_the_old_policy_before_prompt_resolution_finishes() -> None:
    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        old = competition.controller.require()
        started, finish, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = competition.controller.download

        async def download(url: str) -> bytes:
            started.set()
            await finish.wait()
            return await original(url)

        async def closed() -> None:
            notified.set()

        competition.controller.download = download
        competition.controller.on_close = closed
        competition.source.publish(publication(number=1, prompt="lamp"))
        refresh = asyncio.create_task(competition.controller.refresh())
        await asyncio.wait_for(started.wait(), 2)
        assert notified.is_set() and competition.controller.inputs == old
        with pytest.raises(AuthRejected):
            competition.controller.require()
        finish.set()
        await refresh
        assert competition.controller.require().round == "test-1"
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["open", "paused", "collecting", "duels", "finalizing", "finished"])
def test_stage_close_and_resume_uses_fresh_permission_without_changing_frozen_inputs(stage: str) -> None:
    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        frozen = competition.controller.require()
        competition.source.publish(publication(stage=stage))
        await competition.controller.refresh()
        with pytest.raises(AuthRejected, match="round_closed"):
            competition.controller.require()
        assert competition.controller.inputs == frozen
        competition.source.publish(publication())
        await competition.controller.refresh()
        assert competition.controller.require() == frozen
        competition.now[0] += 31
        with pytest.raises(AuthRejected, match="round_policy_stale"):
            competition.controller.require()
    asyncio.run(scenario())


def test_null_eta_does_not_override_the_collectors_block_deadline() -> None:
    async def scenario() -> None:
        competition = Competition(publication(deadline=12))
        await competition.controller.refresh()
        inputs = competition.controller.require()
        assert inputs.generation_deadline_block == 12
        assert competition.controller.policy is not None
        assert competition.controller.policy.expires_at == competition.now[0] + 24
        competition.block[0] = 12
        await competition.controller.refresh()
        with pytest.raises(AuthRejected):
            competition.controller.require()
    asyncio.run(scenario())


def test_recovery_identity_can_remain_fresh_after_a_finite_generation_deadline() -> None:
    async def scenario() -> None:
        competition = Competition(publication(deadline=12))
        competition.block[0] = 12
        await competition.controller.refresh()
        with pytest.raises(AuthRejected):
            competition.controller.require()
        assert competition.controller.require(generation=False).generation_deadline_block == 12
        competition.now[0] += 31
        with pytest.raises(AuthRejected):
            competition.controller.require(generation=False)
    asyncio.run(scenario())


def test_a_controller_restart_restores_the_current_closed_round_for_recovery_without_prompt_downloads() -> None:
    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        frozen = competition.controller.require()
        competition.source.publish(publication(stage="paused"))
        restarted = RoundController(
            git=competition.source, repo=frozen.repository, branch=frozen.branch,
            objects=competition.state, pins=competition.state,
            download=competition.controller.download, head=competition.controller.head,
        )
        before = list(competition.downloaded)
        await restarted.refresh()
        assert restarted.require(generation=False) == frozen and competition.downloaded == before
        with pytest.raises(AuthRejected, match="round_closed"):
            restarted.require()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["seed", "prompts", "missing", "download"])
def test_incomplete_or_changed_same_round_publications_fail_closed_even_after_controller_restart(change: str) -> None:
    async def scenario() -> None:
        competition = Competition()
        await competition.controller.refresh()
        frozen = competition.controller.require()
        files = publication(seed=7) if change == "seed" else publication(prompt="lamp")
        if change == "missing":
            files.pop("rounds/0/seed.json")
        if change == "download":
            files["rounds/0/prompts.txt"] += "\nhttps://images.test/missing.png"
        competition.source.publish(files)
        if change == "seed":
            competition.controller.inputs = None
            competition.controller._resolved.clear()
        with pytest.raises((ValueError, FileNotFoundError, KeyError)):
            await competition.controller.refresh()
        assert competition.controller.policy is None
        with pytest.raises(AuthRejected):
            competition.controller.require()
        assert competition.controller.inputs in (None, frozen)
    asyncio.run(scenario())


def test_repository_relative_prompt_content_is_pinned_to_the_selected_commit() -> None:
    async def scenario() -> None:
        files = publication()
        files["rounds/0/prompts.txt"] = "images/chair.png"
        competition = Competition(files)
        await competition.controller.refresh()
        assert competition.downloaded == [
            "https://raw.githubusercontent.com/404-repo/404-active-competition/" + "a" * 40 + "/images/chair.png"
        ]
    asyncio.run(scenario())


@pytest.mark.parametrize("host,route", [("raw.githubusercontent.com", ""), ("github.com", "blob/")])
@pytest.mark.parametrize("image_branch", ["Persistent/Witness", "persistent/witness"])
def test_repository_image_urls_pin_the_exact_selected_branch_name(host: str, route: str, image_branch: str) -> None:
    async def scenario() -> None:
        files = publication()
        url = f"https://{host}/404-Repo/404-active-competition/{route}{image_branch}/images/chair.png"
        files["rounds/0/prompts.txt"] = url
        competition = Competition(files, branch="Persistent/Witness")
        await competition.controller.refresh()
        expected = (
            "https://raw.githubusercontent.com/404-repo/404-active-competition/" + "a" * 40 + "/images/chair.png"
            if image_branch == "Persistent/Witness" else url
        )
        assert competition.downloaded == [expected]
    asyncio.run(scenario())


def test_frozen_prompt_objects_keep_their_image_media_type() -> None:
    from test_state import FakeS3
    from pod_gateway.rounds import RoundImages
    from pod_gateway.state import R2StateStore

    s3 = FakeS3()
    images = RoundImages(R2StateStore(s3, "private"))
    content = png()
    images.put("journal", content, "image/png")
    assert images.get("journal") == content
    assert s3.puts[0]["ContentType"] == "image/png"


def test_missing_explicit_deadline_and_invalid_images_are_not_unbounded_rounds() -> None:
    async def scenario() -> None:
        files = publication()
        files["rounds/0/schedule.json"] = '{"earliest_reveal_block": 1, "latest_reveal_block": 2}'
        competition = Competition(files)
        with pytest.raises(ValueError):
            await competition.controller.refresh()
        competition = Competition()
        competition.images["chair"] = b"not an image"
        with pytest.raises(OSError):
            await competition.controller.refresh()
        assert competition.controller.policy is None
    asyncio.run(scenario())


def test_resolved_worker_spec_needs_no_manual_round_data_and_keeps_github_credentials_at_gateway(
    values: dict[str, object],
) -> None:
    async def scenario() -> None:
        values.pop("WITNESS_ACTIVE_ROUND")
        values.pop("WITNESS_ROUND_END")
        values["WITNESS_GITHUB_TOKEN"] = "fixture-read-only-token"
        values["WITNESS_COMPETITION_BRANCH"] = "persistent/witness"
        values["WITNESS_RENDERER_PROFILE"] = json.dumps({
            "image_digest": "sha256:" + "a" * 64, "validation_sha256": "b" * 64,
        })
        values["DINOV3_REVISION"] = "fixture-model"
        settings = Settings.model_validate(values)
        competition = Competition()
        await competition.controller.refresh()
        inputs = competition.controller.require()
        spec = worker_spec(settings, inputs)
        frozen = RoundInputs.model_validate_json(spec.env["WITNESS_ROUND_INPUTS"])
        artifacts = ArtifactConfig.model_validate_json(spec.env["WITNESS_ARTIFACTS"])
        assert frozen == inputs and artifacts.seed == inputs.seed and artifacts.prompt_ids == inputs.prompt_ids
        assert artifacts.commits == {}
        assert "WITNESS_GITHUB_TOKEN" not in spec.env
        assert gateway_spec(settings).env["WITNESS_GITHUB_TOKEN"] == "fixture-read-only-token"
        competition.source.publish(publication(number=1, seed=99, prompt="lamp"))
        await competition.controller.refresh()
        assert RoundInputs.model_validate_json(spec.env["WITNESS_ROUND_INPUTS"]) == frozen
        assert worker_spec(settings, competition.controller.require()).fingerprint != spec.fingerprint
    asyncio.run(scenario())


def test_automatic_round_configuration_does_not_enable_a_test_hotkey_exemption(values: dict[str, object]) -> None:
    values["WITNESS_TEST_HOTKEY"] = values["WITNESS_GATEWAY_OWNER_HOTKEY"]
    values["WITNESS_COMPETITION_BRANCH"] = "test"
    with pytest.raises(ValueError, match="Finney membership"):
        Settings.model_validate(values)


def test_storage_namespaces_keep_results_and_call_journals_separate_with_unchanged_official_paths() -> None:
    from subnet_common.competition.generations import GenerationResult
    from pod_gateway.records import CallRecords
    from test_recording import invocation

    state = MemoryState()
    digest = "d" * 64
    for prefix, reason in (("", "official"), ("isolated/", "test")):
        store = ArtifactRepository(state, SCOPE, index=state, seed=42, prompt_ids={digest: "chair"},
                                   storage_prefix=prefix)
        store.update_result(digest, GenerationResult(failure_reason=reason))
        records = CallRecords(PrefixedState(state, prefix))
        records.reserve(SCOPE, invocation())
        assert state.get(prefix + records.key(SCOPE, invocation().id)) is not None
    for prefix, reason in (("", "official"), ("isolated/", "test")):
        store = ArtifactRepository(state, SCOPE, index=state, seed=42, prompt_ids={digest: "chair"},
                                   storage_prefix=prefix)
        result = store.result(digest)
        assert result is not None and result.failure_reason == reason
        assert store.index_key == prefix + f"rounds/{SCOPE.round}/{SCOPE.hotkey}/submitted.json"


def test_rollover_allocation_waits_for_the_old_pod_to_finish_evidence() -> None:
    provider, drain = Provider(), Drain()
    allocator = Allocator(provider, drain, SPEC)
    previous = allocator.acquire(SCOPE, FIRST)
    drain.ready = False
    next_scope = replace(SCOPE, round="test-1")
    next_session = allocator.acquire(next_scope, SECOND)
    assert next_session.phase == "draining" and previous.pod_id in provider.pods and provider.creates == 1
    drain.ready = True
    reconciled = allocator.reconcile(next_scope)
    assert reconciled is not None and reconciled.phase == "allocated" and provider.creates == 2


def test_automatic_revivor_does_not_retire_a_paused_or_previous_round() -> None:
    from test_revivor import GATEWAY, Published
    provider = Provider()
    workers = Allocator(provider, Drain(), SPEC)
    old = workers.acquire(SCOPE, FIRST)
    revivor = Revivor(provider=provider, gateway=Allocator(provider, Drain(), SPEC), workers=workers,
                      scope=GATEWAY, active_round="unconfigured", publisher=Published(), probe=lambda _: True,
                      follow_competition=True, round_end=1)
    revivor.cleanup_workers()
    assert old.pod_id in provider.pods


def test_rollover_frees_the_fifo_slot_but_keeps_uncertain_creates_in_capacity_accounting() -> None:
    from pod_gateway.admission import StartQueue
    state, now = MemoryState(), [1000.0]
    queue = StartQueue(state, SCOPE.deployment, interval=0, clock=lambda: now[0])
    queue.enqueue(SCOPE, FIRST, None)
    queue.authorize(SCOPE, FIRST)
    queue.rollover("test-1")
    following = replace(SCOPE, round="test-1")
    queue.enqueue(following, SECOND, None)
    restarted = StartQueue(state, SCOPE.deployment, clock=lambda: now[0])
    restarted.load()
    assert restarted.pending(following) is not None
    assert restarted.unresolved() == {(SCOPE, FIRST)}
    now[0] += 601
    assert restarted.unresolved() == set()


def test_unrecognized_generation_stage_cannot_enable_work() -> None:
    async def scenario() -> None:
        competition = Competition(publication(stage="generating"))
        with pytest.raises(ValueError):
            await competition.controller.refresh()
        with pytest.raises(AuthRejected):
            competition.controller.require()
    asyncio.run(scenario())
