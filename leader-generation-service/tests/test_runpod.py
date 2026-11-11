import asyncio
import json
import threading
from decimal import Decimal
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import make_mocked_request
from conftest import access
from pod_gateway.runpod import CreateRejected, CreateUnknown
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.witness.models import Pod

from leader_generation_service import runpod, server
from leader_generation_service.generation_iteration import generation_iteration
from leader_generation_service.models import Job
from leader_generation_service.store import JobStore, exclusive_lock
from leader_generation_service.witness import SavedResults


class Provider:
    def __init__(self):
        self.pods = {}
        self.creates = 0
        self.deleted = []
        self.lose_reply = False
        self.absent_reply = False
        self.bodies = []
        self.events = []

    def create(self, body):
        self.creates += 1
        self.bodies.append(body)
        self.events.append("create")
        pod = Pod(f"pod-{self.creates}", body["name"], body["image"], "RUNNING", {})
        if not self.absent_reply:
            self.pods[pod.id] = pod
        if self.lose_reply or self.absent_reply:
            raise CreateUnknown("fixture lost reply")
        return pod

    def list(self, fresh=False):
        return list(self.pods.values())

    def get(self, pod_id):
        return self.pods.get(pod_id)

    def delete(self, pod_id):
        self.deleted.append(pod_id)
        self.events.append("delete")
        self.pods.pop(pod_id, None)

    def rename(self, pod_id, name):
        raise AssertionError("No renames are needed")


@pytest.fixture
def runner(scenario, settings):
    provider = Provider()
    price = {
        "cpus": [
            {
                "id": "cpu3c",
                "availability": "HIGH",
                "ramGbPerVcpu": 2,
                "vcpu": {"min": 2, "max": 32},
                "price": {"securePerVcpu": "0.01"},
                "dataCenters": [{"id": "fixture", "availability": "HIGH"}],
            }
        ]
    }
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=price)))
    value = runpod.RunpodRunner(settings, scenario.deps.store, provider, http, shutdown=scenario.controller.shutdown)
    return value, provider


async def launch(value, job):
    await value.ensure(job)
    await value.start(job, access(4_000_000_000))


@pytest.fixture
def control(scenario, settings, runner):
    value, _ = runner
    scenario.deps.runner = value
    value.store.save(Job(snapshot=scenario.snapshot, control_token="c" * 43))
    shutdown = scenario.controller.shutdown
    app = server.application(scenario.controller, settings)

    async def request(action, *, token="c" * 43, method=None):
        method = method or ("GET" if action in ("status", "credentials", "identity", "job") else "POST")
        path = f"/v1/jobs/1/{action}"
        headers = {"Authorization": f"Bearer {token}"}
        initial = make_mocked_request(method, path, headers=headers, app=app)
        match = await app.router.resolve(initial)
        request = make_mocked_request(method, path, headers=headers, app=app, match_info=match)
        return await match.handler(request)

    return request, shutdown


@pytest.fixture
async def pending_create(control, runner, monkeypatch):
    value, provider = runner
    loop = asyncio.get_running_loop()
    pending = SimpleNamespace(
        created=asyncio.Event(),
        finished=asyncio.Event(),
        release=threading.Event(),
        unknown=False,
    )
    create, start = provider.create, value._start

    def blocked_create(body):
        pod = create(body)
        loop.call_soon_threadsafe(pending.created.set)
        if not pending.release.wait(30):
            raise RuntimeError("Fixture provider reply was not released")
        if pending.unknown:
            raise CreateUnknown("Fixture lost create reply")
        return pod

    def observe_start(job, credentials):
        try:
            start(job, credentials)
        finally:
            loop.call_soon_threadsafe(pending.finished.set)

    monkeypatch.setattr(provider, "create", blocked_create)
    monkeypatch.setattr(value, "_start", observe_start)
    return pending


@pytest.mark.parametrize("unknown_reply", [False, True])
async def test_cancelled_create_finishes_before_a_queued_termination(
    control, pending_create, runner, scenario, settings, unknown_reply
):
    request, _ = control
    value, provider = runner
    pending_create.unknown = unknown_reply
    operation = asyncio.create_task(request("start"))
    termination = None
    try:
        await asyncio.wait_for(pending_create.created.wait(), timeout=2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert scenario.deps.lifecycle.locked()
        operation.cancel()
        termination = asyncio.create_task(request("terminate"))
        await asyncio.sleep(0)
        assert not termination.done()
        pending_create.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert (await termination).status == 200
        assert value.store.load(1).stopped is True
        assert value.store.load(1).allocation.phase == "terminated"
        assert provider.pods == {}
        await generation_iteration(scenario.controller, settings)
        assert provider.creates == 1
    finally:
        pending_create.release.set()
        await asyncio.wait_for(pending_create.finished.wait(), timeout=2)
        await asyncio.gather(operation, *([] if termination is None else [termination]), return_exceptions=True)


async def test_shutdown_during_final_chain_check_skips_create(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    head = scenario.deps.head
    shutdown = scenario.controller.shutdown

    async def check_head():
        job = value.store.load(1)
        if job is not None and job.allocation is not None:
            shutdown.request_shutdown()
        return await head()

    monkeypatch.setattr(scenario.deps, "head", check_head)
    await generation_iteration(scenario.controller, settings)

    assert shutdown.should_stop
    assert provider.creates == 0
    allocation = value.store.load(1).allocation
    assert allocation.phase == "intent"
    assert allocation.requested_at is None


async def test_shutdown_during_create_journal_write_skips_create(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    loop = asyncio.get_running_loop()
    recorded, release = asyncio.Event(), threading.Event()
    save = value.store.save

    def save_creating(job):
        save(job)
        if job.allocation is not None and job.allocation.phase == "creating":
            loop.call_soon_threadsafe(recorded.set)
            assert release.wait(5), "Fixture journal write was not released"

    monkeypatch.setattr(value.store, "save", save_creating)
    operation = asyncio.create_task(generation_iteration(scenario.controller, settings))
    try:
        await asyncio.wait_for(recorded.wait(), timeout=5)
        scenario.controller.shutdown.request_shutdown()
    finally:
        release.set()
        await operation

    assert provider.creates == 0
    allocation = value.store.load(1).allocation
    assert allocation.phase == "intent"
    assert allocation.requested_at is None


async def test_http_shutdown_drains_provider_work_before_clients_and_state_lock_close(
    control, pending_create, runner, scenario, settings, unused_tcp_port
):
    request, _ = control
    value, provider = runner
    settings.credentials_port = unused_tcp_port
    shutdown = scenario.controller.shutdown
    ready, stop = asyncio.Event(), asyncio.Event()
    lock_path = settings.state_directory / ".lock"

    async def host():
        with exclusive_lock(lock_path):
            async with server.serve_credentials(scenario.controller, settings):
                ready.set()
                await stop.wait()
            value.http.close()

    host_task = asyncio.create_task(host())
    operation = None
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        async with aiohttp.ClientSession(trust_env=False) as client:
            operation = asyncio.create_task(
                client.post(
                    f"http://127.0.0.1:{unused_tcp_port}/v1/jobs/1/start",
                    headers={"Authorization": "Bearer " + "c" * 43},
                    json={},
                )
            )
            await asyncio.wait_for(pending_create.created.wait(), timeout=2)
            shutdown.request_shutdown()
            stop.set()
            with pytest.raises(aiohttp.ClientConnectionError):
                await asyncio.wait_for(asyncio.shield(operation), timeout=15)
            assert not host_task.done()
            assert scenario.deps.lifecycle.locked()
            assert not value.http.is_closed
            with pytest.raises(RuntimeError, match="Another service"):
                with exclusive_lock(lock_path):
                    pytest.fail("State lock was released before the provider reply")
            pending_create.release.set()
            await asyncio.wait_for(host_task, timeout=2)
            await asyncio.gather(operation, return_exceptions=True)
        assert value.http.is_closed
        with exclusive_lock(lock_path):
            pass
        assert value.store.load(1).allocation.phase == "active"
        assert (await request("terminate")).status == 200
        await generation_iteration(scenario.controller, settings)
        assert value.store.load(1).stopped is True
        assert provider.creates == 1
    finally:
        pending_create.release.set()
        stop.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await asyncio.gather(host_task, return_exceptions=True)
        await asyncio.wait_for(pending_create.finished.wait(), timeout=2)


async def test_control_start_keeps_the_pod_and_resume_replaces_after_the_cooldown(control, runner, clock):
    request, _ = control
    value, provider = runner
    assert (await request("start")).status == 200
    first = value.store.load(1)
    assert (await request("start")).status == 200
    assert provider.creates == 1
    clock[0] += 1200
    response = await request("resume")
    assert response.status == 200
    second = value.store.load(1)
    assert provider.events == ["create", "delete", "create"]
    assert second.allocations[0].phase == "terminated"
    assert second.allocation.token != first.allocation.token
    assert second.control_token == first.control_token
    assert json.loads(response.body) == {
        "pod_id": second.allocation.pod_id,
        "name": second.allocation.name,
        "phase": "running",
        "stopped": False,
        "published": False,
    }


async def test_control_terminate_stays_stopped_until_explicit_start(control, scenario, settings, runner, clock):
    request, _ = control
    value, provider = runner
    await request("start")
    response = await request("terminate")
    assert response.status == 200
    assert value.store.load(1).stopped is True
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1
    assert provider.pods == {}
    clock[0] += 1200
    assert (await request("start")).status == 200
    assert value.store.load(1).stopped is False
    assert provider.creates == 2


async def test_stopped_cleanup_retries_during_cdn_outage_and_still_publishes(
    control, scenario, settings, runner, monkeypatch
):
    request, _ = control
    value, provider = runner
    await request("start")
    pod_id = value.store.load(1).allocation.pod_id
    original_delete = provider.delete
    attempts = []

    def delete(identifier):
        attempts.append(identifier)
        if len(attempts) == 1:
            raise RuntimeError("Fixture transient provider outage")
        original_delete(identifier)

    monkeypatch.setattr(provider, "delete", delete)
    assert (await request("terminate")).status == 503
    assert value.store.load(1).stopped is True
    saved = scenario.deps.saved
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        with pytest.raises(httpx.HTTPStatusError):
            await generation_iteration(scenario.controller, settings)
        assert attempts == [pod_id, pod_id]
        assert provider.pods == {}
        assert value.store.load(1).allocation.phase == "terminated"
        assert value.store.load(1).published is False
    scenario.deps.saved = saved
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).published is True
    assert value.store.load(1).stopped is True
    assert provider.creates == 1


async def test_stopped_completed_results_publish_after_cleanup_recovers(
    control, scenario, settings, runner, monkeypatch
):
    request, _ = control
    value, provider = runner
    await request("start")
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}
    delete = provider.delete

    def unavailable(identifier):
        raise RuntimeError("Fixture provider deletion unavailable")

    monkeypatch.setattr(provider, "delete", unavailable)
    assert (await request("terminate")).status == 503
    with pytest.raises(RuntimeError, match="deletion unavailable"):
        await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).published is False
    monkeypatch.setattr(provider, "delete", delete)
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).published is True
    assert value.store.load(1).stopped is True
    assert set(json.loads(scenario.deps.git.committed["rounds/1/leader/generated.json"])) == set(scenario.images)
    assert provider.creates == 1


@pytest.mark.parametrize("action", ["start", "resume", "status", "terminate"])
async def test_invalid_control_tokens_are_rejected_while_lifecycle_is_busy(control, scenario, action):
    request, _ = control
    async with scenario.deps.lifecycle:
        operation = asyncio.create_task(request(action, token=f"invalid-{action}"))
        try:
            response = await asyncio.wait_for(asyncio.shield(operation), timeout=0.25)
            assert response.status == 401
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("action", ["start", "resume", "terminate", "status"])
async def test_worker_token_cannot_control_the_job(control, runner, action):
    request, _ = control
    value, provider = runner
    await request("start")
    response = await request(action, token=value.store.load(1).allocation.token)
    assert response.status == 401
    assert provider.creates == 1
    assert provider.deleted == []


@pytest.mark.parametrize("resource", ["credentials", "identity", "job"])
async def test_control_token_cannot_read_worker_secrets(control, resource):
    request, _ = control
    await request("start")
    assert (await request(resource)).status == 401


async def test_controls_and_iteration_share_lock_while_credentials_remain_available(
    control, scenario, settings, runner
):
    request, _ = control
    value, provider = runner
    await request("start")
    async with scenario.deps.lifecycle:
        iteration = asyncio.create_task(generation_iteration(scenario.controller, settings))
        command = asyncio.create_task(request("start"))
        await asyncio.sleep(0)
        assert not iteration.done()
        assert not command.done()
        credentials = await request("credentials", token=value.store.load(1).allocation.token)
        assert credentials.status == 200
    await asyncio.gather(iteration, command)
    assert provider.creates == 1


async def test_closed_round_or_shutdown_rejects_new_control_work(control, scenario, runner):
    request, shutdown = control
    value, provider = runner
    await request("start")
    original_id = value.store.load(1).allocation.id
    scenario.state["block"] = 100
    assert (await request("resume")).status == 409
    assert value.store.load(1).allocation.id == original_id
    assert provider.deleted == []
    shutdown.request_shutdown()
    assert (await request("start")).status == 503
    assert provider.creates == 1


async def test_control_resume_reconciles_unknown_create_before_replacement(control, runner):
    request, _ = control
    value, provider = runner
    provider.absent_reply = True
    assert (await request("start")).status == 503
    assert (await request("resume")).status == 503
    assert provider.creates == 1
    assert value.store.load(1).allocation.phase == "creating"


async def test_status_returns_current_provider_phase_without_starting_work(control, runner):
    request, _ = control
    value, provider = runner
    await request("start")
    pod = provider.pods[value.store.load(1).allocation.pod_id]
    provider.pods[pod.id] = Pod(pod.id, pod.name, pod.image, "EXITED", {})
    response = await request("status")
    assert response.status == 200
    assert json.loads(response.body)["phase"] == "exited"
    assert provider.creates == 1
    assert provider.deleted == []


async def test_restart_adopts_a_lost_create_reply_and_replaces_only_after_a_stall(scenario, settings, runner, clock):
    value, provider = runner
    provider.lose_reply = True
    job = Job(snapshot=scenario.snapshot)
    with pytest.raises(CreateUnknown):
        await launch(value, job)
    recorded = value.store.load(1)
    assert recorded.allocation.phase == "creating"
    assert recorded.allocation.pod_id is None
    provider.lose_reply = False
    scenario.deps.runner = value
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create"]
    assert value.store.load(1).allocation.pod_id == "pod-1"
    clock[0] += settings.stall_replacement_seconds - 1200
    await generation_iteration(scenario.controller, settings)
    current = value.store.load(1)
    assert provider.events == ["create", "delete", "create"]
    assert current.allocations[0].pod_id == "pod-1"
    assert current.allocations[0].phase == "terminated"
    assert current.allocation.pod_id == "pod-2"


async def test_unconfirmed_absence_does_not_repeat_create(scenario, runner):
    value, provider = runner
    provider.absent_reply = True
    job = Job(snapshot=scenario.snapshot)
    with pytest.raises(CreateUnknown):
        await launch(value, job)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await value.terminate(value.store.load(1))
    assert provider.creates == 1


@pytest.mark.parametrize("failure", ["unconfirmed_create", "cleanup"])
async def test_old_job_must_be_disposed_before_starting_the_next_round(
    scenario, settings, runner, monkeypatch, clock, failure
):
    value, provider = runner
    delete = provider.delete
    old = Job(snapshot=scenario.snapshot)
    if failure == "unconfirmed_create":
        provider.absent_reply = True
        with pytest.raises(CreateUnknown):
            await launch(value, old)
        provider.absent_reply = False
    else:
        await launch(value, old)
        old.published = True
        value.store.save(old)

        def unavailable(pod_id):
            if pod_id == old.allocation.pod_id:
                raise RuntimeError("Fixture old Pod deletion unavailable")
            delete(pod_id)

        monkeypatch.setattr(provider, "delete", unavailable)
    original_allocation = old.allocation.model_copy(deep=True)
    git = scenario.deps.git
    for path, content in list(git.files.items()):
        if path.startswith("rounds/1/"):
            git.files[path.replace("rounds/1/", "rounds/2/")] = content
    git.files["state.json"] = CompetitionState(current_round=2, stage=RoundStage.MINER_GENERATION).model_dump_json()

    async def inputs():
        return scenario.inputs.model_copy(update={"number": 2, "round": "test-2"})

    scenario.deps.inputs = inputs
    scenario.deps.runner = value

    def saved(request):
        assert request.url.path in (
            f"/sandbox/rounds/test-1/{scenario.snapshot.hotkey}/submitted.json",
            f"/sandbox/rounds/test-2/{scenario.snapshot.hotkey}/submitted.json",
        )
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(saved)) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        with pytest.raises(RuntimeError):
            await generation_iteration(scenario.controller, settings)
        assert value.store.load(2) is None
        assert value.store.load(1).allocation == original_allocation
        assert provider.creates == 1
        if failure == "unconfirmed_create":
            provider.pods["pod-1"] = Pod(
                "pod-1", old.allocation.name, scenario.snapshot.generation_image, "RUNNING", {}
            )
        else:
            monkeypatch.setattr(provider, "delete", delete)
        clock[0] += 1200
        await generation_iteration(scenario.controller, settings)
        allocation = value.store.load(2).allocation
        assert allocation.phase == "active"
        assert provider.creates == 2
    assert provider.creates == 2
    assert value.store.load(2).allocation.id == allocation.id
    assert value.store.load(1).allocation.phase == "terminated"
    assert value.store.load(1).snapshot == scenario.snapshot


async def test_starting_an_old_job_only_cleans_up_its_pod(control, scenario, runner):
    request, _ = control
    value, provider = runner
    await request("start")
    scenario.deps.git.files["state.json"] = CompetitionState(
        current_round=2, stage=RoundStage.MINER_GENERATION
    ).model_dump_json()
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}
    reads = scenario.state["index_reads"]
    assert (await request("start")).status == 200
    assert scenario.state["index_reads"] == reads
    assert value.store.load(1).allocation.phase == "terminated"
    assert provider.creates == 1


@pytest.mark.parametrize("already_deleted", [False, True])
async def test_new_round_starts_after_old_pod_cleanup_without_reading_old_results(
    scenario, settings, runner, clock, already_deleted
):
    value, provider = runner
    old = Job(snapshot=scenario.snapshot)
    await launch(value, old)
    if already_deleted:
        await value.terminate(old)
    git = scenario.deps.git
    for path, content in list(git.files.items()):
        if path.startswith("rounds/1/"):
            git.files[path.replace("rounds/1/", "rounds/2/")] = content
    git.files["state.json"] = CompetitionState(current_round=2, stage=RoundStage.MINER_GENERATION).model_dump_json()

    async def inputs():
        return scenario.inputs.model_copy(update={"number": 2, "round": "test-2"})

    reads = []

    def saved(request):
        reads.append(request.url.path)
        return httpx.Response(503 if "/test-1/" in request.url.path else 200, json={})

    scenario.deps.inputs = inputs
    scenario.deps.runner = value
    clock[0] += 1200
    async with httpx.AsyncClient(transport=httpx.MockTransport(saved)) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).allocation.phase == "terminated"
    assert value.store.load(2).allocation.phase == "active"
    assert provider.creates == 2
    assert reads == [f"/sandbox/rounds/test-2/{scenario.snapshot.hotkey}/submitted.json"]


@pytest.mark.parametrize("failed_read", ["ref", "state"])
async def test_git_outage_defers_results_until_the_current_round_is_known(
    scenario, settings, runner, monkeypatch, failed_read
):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await launch(value, job)
    scenario.deps.runner = value
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}
    git = scenario.deps.git
    original_ref, original_file = git.get_ref_sha, git.get_file

    async def get_ref(ref):
        raise RuntimeError("Fixture Git read unavailable")

    async def get_file(path, ref):
        if path == "state.json":
            raise RuntimeError("Fixture Git read unavailable")
        return await original_file(path, ref)

    monkeypatch.setattr(
        git, "get_ref_sha" if failed_read == "ref" else "get_file", get_ref if failed_read == "ref" else get_file
    )
    with pytest.raises(RuntimeError, match="Git read unavailable"):
        await generation_iteration(scenario.controller, settings)
    assert provider.deleted == []
    assert value.store.load(1).allocation.phase == "active"
    assert value.store.load(1).published is False
    assert scenario.state["index_reads"] == 0
    assert provider.creates == 1
    monkeypatch.setattr(git, "get_ref_sha", original_ref)
    monkeypatch.setattr(git, "get_file", original_file)
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).published is True
    assert provider.creates == 1
    assert set(json.loads(git.committed["rounds/1/leader/generated.json"])) == set(scenario.images)


async def test_known_lost_runner_is_replaced_with_same_snapshot(scenario, settings, runner, clock):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await launch(value, job)
    provider.pods.clear()
    scenario.deps.runner = value
    clock[0] += settings.stall_replacement_seconds
    await generation_iteration(scenario.controller, settings)
    job = value.store.load(1)
    assert provider.creates == 2
    assert len(job.allocations) == 2
    assert job.allocations[0].phase == "terminated"
    assert job.snapshot == scenario.snapshot
    await value.terminate(job)
    assert provider.deleted == ["pod-2"]


async def test_price_ceiling_blocks_create(scenario, runner):
    value, provider = runner
    value.settings.max_hourly_cost = Decimal("0.001")
    with pytest.raises(RuntimeError, match="cost ceiling"):
        await launch(value, Job(snapshot=scenario.snapshot))
    assert provider.creates == 0


@pytest.mark.parametrize("go_memory_mib", [14 * 1024, 16 * 1024])
async def test_price_check_requires_ram_for_the_prover_and_worker(scenario, runner, go_memory_mib):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot.model_copy(update={"go_memory_mib": go_memory_mib}))
    await value.ensure(job)
    if go_memory_mib == 14 * 1024:
        await value.start(job, access(4_000_000_000))
        assert provider.creates == 1
    else:
        with pytest.raises(RuntimeError, match="RAM"):
            await value.start(job, access(4_000_000_000))
        assert provider.creates == 0


async def test_price_check_uses_the_prepared_request_resources(scenario, runner):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await value.ensure(job)
    value.settings.vcpus = 4
    value.settings.go_cpus = 4
    value.settings.max_hourly_cost = Decimal("0.06")
    with pytest.raises(RuntimeError, match="cost ceiling"):
        await value.start(job, access(4_000_000_000))
    assert provider.creates == 0


@pytest.mark.parametrize("phase", ["EXITED", "FAILED"])
async def test_broken_pod_is_replaced_after_a_stall_and_preserves_witness_progress(
    scenario, settings, runner, clock, phase
):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    previous = value.store.load(1)
    config = runpod.read_config(value.store, previous)
    scenario.state["saved"] = {"chair": {"failure_reason": "generation_failed"}}
    pod = provider.pods[previous.allocation.pod_id]
    provider.pods[pod.id] = Pod(pod.id, pod.name, pod.image, phase, {})
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1
    clock[0] += settings.stall_replacement_seconds
    await generation_iteration(scenario.controller, settings)
    resumed = value.store.load(1)
    assert provider.events == ["create", "delete", "create"]
    assert resumed.allocations[0].phase == "terminated"
    assert resumed.allocation.id != previous.allocation.id
    assert resumed.allocation.token != previous.allocation.token
    assert resumed.snapshot == previous.snapshot
    assert runpod.read_config(value.store, resumed) == config
    assert scenario.state["saved"] == {"chair": {"failure_reason": "generation_failed"}}
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 2


async def test_account_and_pod_identity_mismatch_refuse_operation(scenario, runner):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await value.ensure(job)
    with pytest.raises(RuntimeError, match="Provider account"):
        await value.start(job, access(4_000_000_000, "def456"))
    assert provider.creates == 0
    await value.start(job, access(4_000_000_000))
    provider.pods["pod-1"] = Pod("pod-1", "another-runner", scenario.snapshot.generation_image, "RUNNING", {})
    with pytest.raises(RuntimeError, match="identity differs"):
        await value.terminate(job)
    assert provider.deleted == []


async def test_start_request_uses_frozen_source_inputs_and_image(scenario, settings, runner):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await launch(value, job)
    config = runpod.read_config(value.store, job)
    assert config.source_repository == "https://github.com/test/incumbent"
    assert config.source_commit == scenario.snapshot.leader.commit
    assert config.generation_image == scenario.snapshot.generation_image
    assert provider.bodies[0]["image"] == config.generation_image
    assert config.round_inputs_fingerprint == scenario.inputs.fingerprint
    assert config.prompts == {prompt.id: prompt.sha256 for prompt in scenario.inputs.prompts}
    assert config.seed == scenario.inputs.seed
    assert config.hotkey == scenario.snapshot.hotkey
    assert config.account_id == scenario.snapshot.account_id
    assert config.credentials_url == "https://leader.test/v1/jobs/1"
    assert provider.bodies[0]["name"] == job.allocation.name


@pytest.mark.parametrize("interval", [1200, 1800])
async def test_restart_and_manual_start_cannot_bypass_the_allocation_interval(
    control, runner, scenario, settings, clock, interval
):
    request, _ = control
    value, provider = runner
    settings.progress_interval_seconds = interval
    assert (await request("start")).status == 200
    assert (await request("terminate")).status == 200
    clock[0] += interval - 1
    store = JobStore(settings.state_directory)
    scenario.deps.store = store
    scenario.deps.runner = runpod.RunpodRunner(
        settings, store, provider, value.http, shutdown=scenario.controller.shutdown
    )
    await generation_iteration(scenario.controller, settings)
    assert not provider.pods
    assert provider.creates == 1
    assert (await request("start")).status == 200
    assert provider.creates == 1
    clock[0] += 1
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 2
    assert len(provider.pods) == 1


async def test_rejected_allocation_is_also_rate_limited(control, runner, scenario, settings, clock, monkeypatch):
    request, _ = control
    _, provider = runner
    requests = []

    def reject(body):
        requests.append(body)
        raise CreateRejected("Fixture rejected allocation")

    monkeypatch.setattr(provider, "create", reject)
    assert (await request("start")).status == 503
    await generation_iteration(scenario.controller, settings)
    assert len(requests) == 1
    clock[0] += 1200
    with pytest.raises(CreateRejected):
        await generation_iteration(scenario.controller, settings)
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["unavailable", "unconfirmed"])
async def test_cleanup_failure_prevents_replacement_of_a_stalled_pod(
    control, runner, scenario, settings, clock, monkeypatch, failure
):
    request, _ = control
    value, provider = runner
    await request("start")
    reads = scenario.state["index_reads"]

    def unavailable(identifier):
        if failure == "unavailable":
            raise RuntimeError("Fixture deletion unavailable")

    monkeypatch.setattr(provider, "delete", unavailable)
    clock[0] += settings.stall_replacement_seconds
    with pytest.raises(RuntimeError, match="deletion unavailable|termination is not confirmed"):
        await generation_iteration(scenario.controller, settings)
    assert scenario.state["index_reads"] == reads + 1
    assert provider.creates == 1
    assert value.store.load(1).allocation.phase == "active"


async def test_allocation_interval_applies_across_rounds(control, runner, scenario, settings, clock):
    request, _ = control
    value, provider = runner
    await request("start")
    git = scenario.deps.git
    for path, content in list(git.files.items()):
        if path.startswith("rounds/1/"):
            git.files[path.replace("rounds/1/", "rounds/2/")] = content
    git.files["state.json"] = CompetitionState(current_round=2, stage=RoundStage.MINER_GENERATION).model_dump_json()

    async def inputs():
        return scenario.inputs.model_copy(update={"number": 2, "round": "test-2"})

    scenario.deps.inputs = inputs
    clock[0] += 1199
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        await generation_iteration(scenario.controller, settings)
        assert value.store.load(1).allocation.phase == "terminated"
        assert provider.creates == 1
        clock[0] += 1
        await generation_iteration(scenario.controller, settings)
    assert value.store.load(2).allocation.phase == "active"
    assert provider.creates == 2
    assert len(provider.pods) == 1


async def test_closed_round_disposes_running_attempt_without_replacement(control, runner, scenario, settings, clock):
    request, _ = control
    value, provider = runner
    await request("start")
    scenario.state["block"] = 100
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert not provider.pods
    assert provider.creates == 1
    assert value.store.load(1).allocation.phase == "terminated"


@pytest.mark.parametrize("replacement_seconds", [None, 7200], ids=["one-hour-default", "two-hours-configured"])
async def test_no_progress_warns_at_twenty_minutes_and_uses_the_replacement_threshold(
    control, runner, scenario, settings, clock, discord, replacement_seconds
):
    if replacement_seconds is not None:
        settings.stall_replacement_seconds = replacement_seconds
    request, _ = control
    value, provider = runner
    await request("start")
    previous = value.store.load(1).allocation.id
    clock[0] += 1199
    await generation_iteration(scenario.controller, settings)
    assert discord.messages == [] and provider.deleted == []
    clock[0] += 1
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).allocation.id == previous
    assert provider.creates == 1 and provider.deleted == []
    assert len(discord.messages) == 1
    clock[0] += (replacement_seconds or 3600) - 1201
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and provider.deleted == []
    clock[0] += 1
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]
    assert value.store.load(1).allocation.id != previous
    assert len(discord.messages) == 1


async def test_saved_progress_keeps_a_running_generation_pod_and_logs_counts(
    control, runner, scenario, settings, clock, discord
):
    from loguru import logger

    request, _ = control
    value, provider = runner
    await request("start")
    previous = value.store.load(1).allocation.id
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        clock[0] += 1200
        scenario.state["saved"] = {"chair": {"failure_reason": "generation_failed"}}
        await generation_iteration(scenario.controller, settings)
        assert provider.creates == 1 and provider.deleted == []
        assert discord.messages == []
        assert any("1/2" in message and "+1" in message for message in messages)
        clock[0] += 1200
        await generation_iteration(scenario.controller, settings)
        assert value.store.load(1).allocation.id == previous
        assert len(discord.messages) == 1
        clock[0] += 1200
        scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
        await generation_iteration(scenario.controller, settings)
        assert any("2/2" in message and "+1" in message for message in messages)
    finally:
        logger.remove(sink)
    assert value.store.load(1).published
    assert provider.creates == 1 and not provider.pods


async def test_progress_clock_and_warning_survive_service_restart(control, runner, scenario, settings, clock, discord):
    request, _ = control
    value, provider = runner
    await request("start")
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    store = JobStore(settings.state_directory)
    scenario.deps.store = store
    scenario.deps.runner = runpod.RunpodRunner(
        settings, store, provider, value.http, shutdown=scenario.controller.shutdown
    )
    clock[0] += settings.stall_replacement_seconds - 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]
    assert len(discord.messages) == 1


async def test_new_progress_resets_the_stall_timer_and_warning(control, runner, scenario, settings, clock, discord):
    request, _ = control
    value, provider = runner
    await request("start")
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert len(discord.messages) == 1
    clock[0] += 1200
    scenario.state["saved"] = {"chair": {"failure_reason": "generation_failed"}}
    await generation_iteration(scenario.controller, settings)
    assert not value.store.load(1).progress_warning_sent
    assert provider.creates == 1 and provider.deleted == []
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert len(discord.messages) == 2
    assert provider.creates == 1
    clock[0] += settings.stall_replacement_seconds - 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]


async def test_regressing_progress_is_an_error_and_does_not_replace_the_pod(control, runner, scenario, settings, clock):
    request, _ = control
    value, provider = runner
    await request("start")
    clock[0] += 1200
    scenario.state["saved"] = {"chair": {"failure_reason": "generation_failed"}}
    await generation_iteration(scenario.controller, settings)
    progress_at = value.store.load(1).last_progress_at
    clock[0] += settings.stall_replacement_seconds
    scenario.state["saved"] = {}
    with pytest.raises(ValueError, match="count regressed"):
        await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and provider.deleted == []
    assert value.store.load(1).last_progress_at == progress_at
    assert value.store.load(1).completed_prompts == 1


async def test_closed_round_cleans_up_even_when_progress_is_unavailable(control, runner, scenario, settings):
    request, _ = control
    value, provider = runner
    await request("start")
    scenario.state["block"] = 100
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        with pytest.raises(httpx.HTTPStatusError):
            await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and not provider.pods
    assert value.store.load(1).allocation.phase == "terminated"


async def test_failed_progress_read_does_not_kill_a_generation_pod(control, runner, scenario, settings, clock, discord):
    request, _ = control
    value, provider = runner
    await request("start")
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    clock[0] += settings.stall_replacement_seconds - 1200
    saved = scenario.deps.saved
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        with pytest.raises(httpx.HTTPStatusError):
            await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and provider.deleted == []
    assert len(provider.pods) == 1
    scenario.deps.saved = saved
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]


async def test_discord_failure_logs_the_error_type_without_the_webhook(scenario, discord):
    from loguru import logger

    discord.status = 503
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        await scenario.deps.discord.send_embed(title="Fixture warning", color=0)
    finally:
        logger.remove(sink)
    assert len(discord.messages) == 1
    assert any("HTTPStatusError" in message for message in messages)
    assert all("https://discord.test/progress" not in message for message in messages)


async def test_discord_outage_does_not_prevent_stalled_pod_recovery(
    control, runner, scenario, settings, clock, discord
):
    request, _ = control
    _, provider = runner
    await request("start")
    discord.status = 503
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    clock[0] += settings.stall_replacement_seconds - 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]


async def test_render_rejection_is_terminal_progress_and_is_published(
    control, runner, scenario, settings, clock, discord
):
    request, _ = control
    value, provider = runner
    await request("start")
    scenario.state["saved"] = {"chair": {"failure_reason": "render_rejected", "js": "chair.js"}}
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).completed_prompts == 1
    assert not value.store.load(1).published
    assert provider.creates == 1 and provider.deleted == []
    assert discord.messages == []
    scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).published
    assert provider.creates == 1 and not provider.pods
    published = json.loads(scenario.deps.git.committed["rounds/1/leader/generated.json"])
    assert published["chair"]["failure_reason"] == "render_rejected"
    assert published["lamp"]["failure_reason"] == "generation_failed"
