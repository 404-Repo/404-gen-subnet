import asyncio
import base64
import json
import threading
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from bittensor_wallet import Keypair, Wallet
from conftest import access
from loguru import logger
from pod_gateway.runpod import CreateRejected, CreateUnknown
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.witness.models import Pod

from leader_generation_service import runpod
from leader_generation_service.generation_iteration import generation_iteration
from leader_generation_service.models import Job
from leader_generation_service.store import JobStore
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
def cpu_catalog():
    return {
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


@pytest.fixture
def runner(scenario, settings, cpu_catalog):
    wallet = Wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey, path=str(settings.wallet_path))
    wallet.set_hotkey(Keypair.create_from_uri("//Alice"), encrypt=False, overwrite=True)
    provider = Provider()
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=cpu_catalog)))
    value = runpod.RunpodRunner(settings, scenario.deps.store, provider, http, shutdown=scenario.controller.shutdown)
    return value, provider


@pytest.fixture
def runpod_errors():
    errors = []
    sink = logger.add(lambda message: errors.append(message.record["message"]), level="ERROR", filter=runpod.__name__)
    try:
        yield errors
    finally:
        logger.remove(sink)


async def launch(value, job):
    await value.ensure(job)
    await value.start(job, access(4_000_000_000))


@pytest.mark.parametrize("lost_reply", [False, True], ids=["direct-confirmation", "recovered-from-inventory"])
async def test_generation_pod_logs_follow_provider_events(scenario, runner, monkeypatch, lost_reply):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    provider.lose_reply = lost_reply
    events = provider.events
    list_pods, get_pod = provider.list, provider.get

    def read_inventory(*, fresh=False):
        pods = list_pods(fresh=fresh)
        events.append("inventory read")
        return pods

    def check_pod(pod_id):
        pod = get_pod(pod_id)
        if pod is None:
            events.append("absence confirmed")
        return pod

    monkeypatch.setattr(provider, "list", read_inventory)
    monkeypatch.setattr(provider, "get", check_pod)
    sink = logger.add(lambda message: events.append(message.record["message"]), filter=runpod.__name__)
    try:
        if lost_reply:
            with pytest.raises(CreateUnknown):
                await launch(value, job)
            job = value.store.load(1)
            await value.status(job)
        else:
            await launch(value, job)
        await value.terminate(job)
    finally:
        logger.remove(sink)

    name = job.allocation.name
    expected = [f"Round 1 requesting generation pod {name}", "create"]
    if lost_reply:
        expected.extend(
            (
                f"Round 1 creation unconfirmed for generation pod {name}",
                "inventory read",
                "Round 1 recovered generation pod pod-1 from inventory",
            )
        )
    else:
        expected.append("Round 1 created generation pod pod-1")
    assert events == [*expected, "delete", "absence confirmed", "Round 1 deleted generation pod pod-1"]


@pytest.fixture
async def pending_create(runner, monkeypatch):
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

    def observe_start(job, credentials, min_validity_seconds):
        try:
            start(job, credentials, min_validity_seconds)
        finally:
            loop.call_soon_threadsafe(pending.finished.set)

    monkeypatch.setattr(provider, "create", blocked_create)
    monkeypatch.setattr(value, "_start", observe_start)
    return pending


@pytest.mark.parametrize("unknown_reply", [False, True])
async def test_cancelled_iteration_waits_for_the_create_reply(
    pending_create, runner, scenario, settings, unknown_reply
):
    value, provider = runner
    scenario.deps.runner = value
    pending_create.unknown = unknown_reply
    operation = asyncio.create_task(generation_iteration(scenario.controller, settings))
    try:
        await asyncio.wait_for(pending_create.created.wait(), timeout=2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        pending_create.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert value.store.load(1).allocation.phase == ("creating" if unknown_reply else "active")
        assert provider.creates == 1
    finally:
        pending_create.release.set()
        await asyncio.wait_for(pending_create.finished.wait(), timeout=2)
        await asyncio.gather(operation, return_exceptions=True)


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


async def test_stopped_cleanup_retries_after_provider_outage(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    job = value.store.load(1)
    job.stopped = True
    value.store.save(job)
    pod_id = job.allocation.pod_id
    original_delete = provider.delete
    attempts = []

    def delete(identifier):
        attempts.append(identifier)
        if len(attempts) == 1:
            raise RuntimeError("Fixture transient provider outage")
        original_delete(identifier)

    monkeypatch.setattr(provider, "delete", delete)
    with pytest.raises(RuntimeError, match="transient provider outage"):
        await generation_iteration(scenario.controller, settings)
    await generation_iteration(scenario.controller, settings)

    assert attempts == [pod_id, pod_id]
    assert provider.pods == {}
    assert value.store.load(1).allocation.phase == "terminated"
    assert value.store.load(1).stopped is True
    assert provider.creates == 1


async def test_completed_job_retries_pod_cleanup(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}
    delete = provider.delete

    def unavailable(identifier):
        raise RuntimeError("Fixture provider deletion unavailable")

    monkeypatch.setattr(provider, "delete", unavailable)
    with pytest.raises(RuntimeError, match="deletion unavailable"):
        await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).completed
    monkeypatch.setattr(provider, "delete", delete)
    await generation_iteration(scenario.controller, settings)

    assert value.store.load(1).allocation.phase == "terminated"
    assert provider.creates == 1


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
        old.completed_prompts = len(old.snapshot.inputs.prompt_ids)
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

    reads = []

    def saved(request):
        reads.append(request.url.path)
        return httpx.Response(503 if "/test-1/" in request.url.path else 200, json={})

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
    assert value.store.load(1).completed is False
    assert scenario.state["index_reads"] == 0
    assert provider.creates == 1
    monkeypatch.setattr(git, "get_ref_sha", original_ref)
    monkeypatch.setattr(git, "get_file", original_file)
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).completed is True
    assert provider.creates == 1
    assert not provider.pods


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


async def test_price_ceiling_blocks_create(scenario, runner, runpod_errors):
    value, provider = runner
    value.settings.max_hourly_cost = Decimal("0.001")
    with pytest.raises(RuntimeError, match="cost ceiling"):
        await launch(value, Job(snapshot=scenario.snapshot))
    assert provider.creates == 0
    estimate = Decimal("0.08") + Decimal("2") / 730
    assert runpod_errors == [f"RunPod estimated cost of ${estimate}/hour exceeds the $0.001/hour cost ceiling"]


@pytest.mark.parametrize(
    "changes,data_center,reason",
    [
        ({"id": "other"}, None, "RunPod CPU catalog does not list cpu3c for 8 vCPUs"),
        ({"availability": "NONE"}, None, "RunPod CPU cpu3c has no capacity for 8 vCPUs"),
        ({}, "unavailable", "RunPod CPU cpu3c has no capacity for 8 vCPUs in data center unavailable"),
        ({"price": {"securePerVcpu": "invalid"}}, None, "RunPod CPU catalog returned an invalid price for cpu3c"),
        ({"price": {"securePerVcpu": "0"}}, None, "RunPod CPU catalog returned an invalid price for cpu3c"),
        ({"vcpu": {"min": 2, "max": 4}}, None, "RunPod CPU cpu3c supports 2 to 4 vCPUs but 8 were requested"),
    ],
)
async def test_cpu_catalog_block_logs_the_reason(
    scenario, runner, cpu_catalog, runpod_errors, changes, data_center, reason
):
    value, provider = runner
    value.settings.data_center = data_center
    cpu_catalog["cpus"][0].update(changes)

    with pytest.raises(RuntimeError, match="Could not verify RunPod CPU"):
        await launch(value, Job(snapshot=scenario.snapshot))

    assert runpod_errors == [reason]
    assert provider.creates == 0


@pytest.mark.parametrize(
    "status,reason",
    [
        (503, "RunPod CPU catalog request failed (HTTP 503)"),
        (None, "RunPod CPU catalog request failed (ReadTimeout)"),
        (200, "RunPod CPU catalog returned invalid data for cpu3c (JSONDecodeError)"),
    ],
)
async def test_cpu_catalog_request_failure_logs_the_reason(scenario, runner, runpod_errors, status, reason):
    value, provider = runner

    def reply(request):
        if status is None:
            raise httpx.ReadTimeout("fixture catalog details", request=request)
        return httpx.Response(status, text="fixture catalog details")

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        value.http = http
        with pytest.raises(RuntimeError, match="Could not verify RunPod CPU"):
            await launch(value, Job(snapshot=scenario.snapshot))

    assert runpod_errors == [reason]
    assert provider.creates == 0


async def test_launch_waits_for_a_token_that_covers_the_generation_window(scenario, settings, runner, clock):
    value, provider = runner
    scenario.deps.runner = value
    scenario.deps.credentials.load = lambda *_: access(clock[0] + 7200)

    with pytest.raises(RuntimeError, match="sufficient validity"):
        await generation_iteration(scenario.controller, settings)
    assert provider.creates == 0

    credentials = access(clock[0] + 14400)
    scenario.deps.credentials.load = lambda *_: credentials
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1
    environment = provider.bodies[0]["env"]
    hotkey = settings.wallet_path / settings.wallet_name / "hotkeys" / settings.wallet_hotkey
    assert base64.b64decode(environment["LEADER_JOB_HOTKEY"]) == hotkey.read_bytes()
    assert environment["LEADER_JOB_ACCESS_TOKEN"] == credentials.access_token
    journal = (settings.state_directory / "rounds/1.json").read_text()
    assert environment["LEADER_JOB_HOTKEY"] not in journal
    assert environment["LEADER_JOB_ACCESS_TOKEN"] not in journal


async def test_create_rechecks_token_validity_after_preflight(scenario, runner, clock, monkeypatch):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await value.ensure(job)
    min_validity_seconds = 11400
    credentials = access(clock[0] + min_validity_seconds + 1)
    price = value._price

    def slow_price(config):
        clock[0] += 2
        return price(config)

    monkeypatch.setattr(value, "_price", slow_price)
    with pytest.raises(RuntimeError, match="insufficient validity"):
        await value.start(job, credentials, min_validity_seconds=min_validity_seconds)
    assert provider.creates == 0
    allocation = value.store.load(1).allocation
    assert allocation.phase == "intent"
    assert allocation.requested_at is None


@pytest.mark.parametrize("go_memory_mib", [14 * 1024, 16 * 1024])
async def test_price_check_requires_ram_for_the_prover_and_worker(scenario, runner, runpod_errors, go_memory_mib):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot.model_copy(update={"go_memory_mib": go_memory_mib}))
    await value.ensure(job)
    if go_memory_mib == 14 * 1024:
        await value.start(job, access(4_000_000_000))
        assert provider.creates == 1
        assert runpod_errors == []
    else:
        with pytest.raises(RuntimeError, match="RAM"):
            await value.start(job, access(4_000_000_000))
        assert provider.creates == 0
        assert runpod_errors == ["RunPod CPU cpu3c offers 16384 MiB RAM but the worker needs 18432 MiB"]


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


async def test_create_rejects_a_different_hotkey(scenario, settings, runner):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await value.ensure(job)
    wallet = Wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey, path=str(settings.wallet_path))
    wallet.set_hotkey(Keypair.create_from_uri("//Bob"), encrypt=False, overwrite=True)
    with pytest.raises(ValueError, match="Operator hotkey differs"):
        await value.start(job, access(4_000_000_000))
    assert provider.creates == 0


async def test_start_request_passes_the_competition_location_and_round(scenario, settings, runner):
    value, provider = runner
    settings.github_branch = "witness-test"
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    job = value.store.load(1)
    config = runpod.read_config(value.store, job)
    assert config.competition_repository == settings.github_repo
    assert config.competition_branch == settings.github_branch
    assert config.round_number == scenario.inputs.number
    assert config.generation_image == scenario.snapshot.generation_image
    assert provider.bodies[0]["image"] == config.generation_image
    assert config.hotkey == scenario.snapshot.hotkey
    assert config.account_id == scenario.snapshot.account_id
    assert provider.bodies[0]["name"] == job.allocation.name
    assert json.loads(provider.bodies[0]["env"]["LEADER_JOB_CONFIG"]) == json.loads(
        (value.store.run_directory(job) / "config.json").read_bytes()
    )


async def test_prepared_attempt_with_old_payload_uses_the_updated_worker_image(scenario, settings, runner):
    value, provider = runner
    job = Job(snapshot=scenario.snapshot)
    await value.ensure(job)
    allocation_id = job.allocation.id
    config_path = value.store.run_directory(job) / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "generation_image": settings.generation_image,
                "source_repository": "https://github.com/test/incumbent",
                "source_commit": scenario.leader.commit,
                "prompts": {"chair": "c" * 64, "lamp": "e" * 64},
                "seed": 42,
                "hotkey": scenario.snapshot.hotkey,
                "account_id": scenario.snapshot.account_id,
                "round_inputs_fingerprint": "a" * 64,
            }
        )
    )
    settings.generation_image = "registry.test/leader@sha256:" + "e" * 64
    await launch(value, value.store.load(1))
    assert value.store.load(1).allocation.id == allocation_id
    assert provider.bodies[0]["image"] == settings.generation_image
    assert runpod.read_config(value.store, job).round_number == scenario.inputs.number


@pytest.mark.parametrize("interval", [1200, 1800])
async def test_restart_cannot_bypass_the_allocation_interval(runner, scenario, settings, clock, interval):
    value, provider = runner
    settings.progress_interval_seconds = interval
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    await value.terminate(value.store.load(1))
    clock[0] += interval - 1
    store = JobStore(settings.state_directory)
    scenario.deps.store = store
    scenario.deps.runner = runpod.RunpodRunner(
        settings, store, provider, value.http, shutdown=scenario.controller.shutdown
    )
    await generation_iteration(scenario.controller, settings)
    assert not provider.pods
    assert provider.creates == 1
    clock[0] += 1
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 2
    assert len(provider.pods) == 1


async def test_rejected_allocation_is_also_rate_limited(runner, scenario, settings, clock, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    requests = []

    def reject(body):
        requests.append(body)
        raise CreateRejected("Fixture rejected allocation")

    monkeypatch.setattr(provider, "create", reject)
    with pytest.raises(CreateRejected):
        await generation_iteration(scenario.controller, settings)
    await generation_iteration(scenario.controller, settings)
    assert len(requests) == 1
    clock[0] += 1200
    with pytest.raises(CreateRejected):
        await generation_iteration(scenario.controller, settings)
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["unavailable", "unconfirmed"])
async def test_cleanup_failure_prevents_replacement_of_a_stalled_pod(
    runner, scenario, settings, clock, monkeypatch, failure
):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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


async def test_allocation_interval_applies_across_rounds(runner, scenario, settings, clock):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    git = scenario.deps.git
    for path, content in list(git.files.items()):
        if path.startswith("rounds/1/"):
            git.files[path.replace("rounds/1/", "rounds/2/")] = content
    git.files["state.json"] = CompetitionState(current_round=2, stage=RoundStage.MINER_GENERATION).model_dump_json()

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


async def test_closed_round_disposes_running_attempt_without_replacement(runner, scenario, settings, clock):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    scenario.state["block"] = 100
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert not provider.pods
    assert provider.creates == 1
    assert value.store.load(1).allocation.phase == "terminated"


@pytest.mark.parametrize("replacement_seconds", [None, 7200], ids=["one-hour-default", "two-hours-configured"])
async def test_no_progress_warns_at_twenty_minutes_and_uses_the_replacement_threshold(
    runner, scenario, settings, clock, discord, replacement_seconds
):
    if replacement_seconds is not None:
        settings.stall_replacement_seconds = replacement_seconds
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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
    runner, scenario, settings, clock, discord
):
    from loguru import logger

    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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
    assert value.store.load(1).completed
    assert provider.creates == 1 and not provider.pods


async def test_progress_clock_and_warning_survive_service_restart(runner, scenario, settings, clock, discord):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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


async def test_new_progress_resets_the_stall_timer_and_warning(runner, scenario, settings, clock, discord):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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


async def test_regressing_progress_is_an_error_and_does_not_replace_the_pod(runner, scenario, settings, clock):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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


async def test_closed_round_cleans_up_even_when_progress_is_unavailable(runner, scenario, settings):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    scenario.state["block"] = 100
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        with pytest.raises(httpx.HTTPStatusError):
            await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and not provider.pods
    assert value.store.load(1).allocation.phase == "terminated"


async def test_failed_progress_read_does_not_kill_a_generation_pod(runner, scenario, settings, clock, discord):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
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


async def test_discord_outage_does_not_prevent_stalled_pod_recovery(runner, scenario, settings, clock, discord):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    discord.status = 503
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    clock[0] += settings.stall_replacement_seconds - 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]


async def test_render_rejection_is_terminal_progress(runner, scenario, settings, clock, discord):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    scenario.state["saved"] = {"chair": {"failure_reason": "render_rejected", "js": "chair.js"}}
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).completed_prompts == 1
    assert not value.store.load(1).completed
    assert provider.creates == 1 and provider.deleted == []
    assert discord.messages == []
    scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert value.store.load(1).completed
    assert provider.creates == 1 and not provider.pods
