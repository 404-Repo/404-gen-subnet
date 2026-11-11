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
from pod_gateway.runpod import CreateUnknown
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.witness.models import Pod

from leader_generation_service import runpod
from leader_generation_service.generation_iteration import generation_iteration
from leader_generation_service.round_runner import RoundRunner
from leader_generation_service.witness import SavedResults


class Provider:
    def __init__(self):
        self.pods = {}
        self.creates = 0
        self.deleted = []
        self.lose_reply = False
        self.bodies = []
        self.events = []
        self.inventory_reads = []
        self.lookups = []

    def create(self, body):
        self.creates += 1
        self.bodies.append(body)
        self.events.append("create")
        pod = Pod(f"pod-{self.creates}", body["name"], body["image"], "RUNNING", {})
        self.pods[pod.id] = pod
        if self.lose_reply:
            raise CreateUnknown("fixture lost reply")
        return pod

    def list(self, fresh=False):
        self.inventory_reads.append(fresh)
        return list(self.pods.values())

    def get(self, pod_id):
        self.lookups.append(pod_id)
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
    value = runpod.RunpodRunner(settings, provider, http, shutdown=scenario.controller.shutdown)
    return value, provider


@pytest.fixture
def runpod_errors():
    errors = []
    sink = logger.add(lambda message: errors.append(message.record["message"]), level="ERROR", filter=runpod.__name__)
    try:
        yield errors
    finally:
        logger.remove(sink)


async def launch(value, scenario):
    return await value.start(1, scenario.deps.hotkey, access(4_000_000_000))


async def test_startup_retires_generation_pods_by_prefix_without_a_journal(scenario, settings, runner):
    value, provider = runner
    scenario.deps.runner = value
    provider.pods["old"] = Pod("old", "leader-runner-old", settings.generation_image, "RUNNING", {})
    provider.pods["older"] = Pod("older", "leader-runner-older", settings.generation_image, "EXITED", {})
    provider.pods["witness"] = Pod("witness", "witness-live", settings.generation_image, "RUNNING", {})

    await value.cleanup()
    await value.cleanup()
    await generation_iteration(scenario.controller, settings)
    await generation_iteration(scenario.controller, settings)

    assert provider.inventory_reads == [True]
    assert set(provider.deleted) == {"old", "older"}
    assert provider.lookups == provider.deleted
    assert provider.events == ["delete", "delete", "create"]
    assert provider.pods.keys() == {"witness", "pod-1"}


async def test_startup_retries_failed_deletion_without_listing_again(runner, settings, monkeypatch):
    value, provider = runner
    pod = Pod("old", "leader-runner-old", settings.generation_image, "RUNNING", {})
    provider.pods[pod.id] = pod
    delete = provider.delete

    def unavailable(identifier):
        raise RuntimeError("Fixture provider deletion unavailable")

    monkeypatch.setattr(provider, "delete", unavailable)
    with pytest.raises(RuntimeError, match="deletion unavailable"):
        await value.cleanup()
    monkeypatch.setattr(provider, "delete", delete)
    await value.cleanup()
    assert provider.inventory_reads == [True]
    assert provider.deleted == [pod.id]
    assert not provider.pods


async def test_restart_after_a_lost_reply_uses_current_settings_and_saved_progress(scenario, settings, runner):
    value, provider = runner
    scenario.deps.runner = value
    provider.lose_reply = True
    await value.cleanup()
    with pytest.raises(CreateUnknown):
        await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1

    scenario.state["saved"] = {"chair": {"failure_reason": "generation_failed"}}
    settings.witness_url = "https://new-witness.test"
    settings.generation_image = "registry.test/leader@sha256:" + "e" * 64
    restarted = runpod.RunpodRunner(settings, provider, value.http, shutdown=scenario.controller.shutdown)
    scenario.deps.runner = restarted
    controller = RoundRunner(scenario.deps, settings, scenario.controller.shutdown)
    provider.lose_reply = False
    await restarted.cleanup()
    await generation_iteration(controller, settings)

    assert provider.inventory_reads == [True, True]
    assert provider.events == ["create", "delete", "create"]
    assert controller.pod.id == "pod-2"
    assert controller.completed_prompts == 1
    config = json.loads(provider.bodies[-1]["env"]["LEADER_JOB_CONFIG"])
    assert config["witness_url"] == settings.witness_url
    assert provider.bodies[-1]["image"] == settings.generation_image


async def test_generation_pod_logs_follow_provider_events(scenario, runner, monkeypatch):
    value, provider = runner
    events = provider.events
    get_pod = provider.get

    def check_pod(pod_id):
        pod = get_pod(pod_id)
        if pod is None:
            events.append("absence confirmed")
        return pod

    monkeypatch.setattr(provider, "get", check_pod)
    sink = logger.add(lambda message: events.append(message.record["message"]), filter=runpod.__name__)
    try:
        pod = await launch(value, scenario)
        await value.terminate(pod)
    finally:
        logger.remove(sink)

    assert events == [
        f"Round 1 requesting generation pod {pod.name}",
        "create",
        "Round 1 created generation pod pod-1",
        f"Deleting generation pod pod-1 ({pod.name})",
        "delete",
        "absence confirmed",
        f"Deleted generation pod pod-1 ({pod.name})",
    ]


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

    def observe_start(number, hotkey, credentials, min_validity_seconds):
        try:
            return start(number, hotkey, credentials, min_validity_seconds)
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
        assert provider.creates == 1
        assert provider.pods.keys() == {"pod-1"}
    finally:
        pending_create.release.set()
        await asyncio.wait_for(pending_create.finished.wait(), timeout=2)
        await asyncio.gather(operation, return_exceptions=True)


async def test_shutdown_during_final_chain_check_skips_create(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    head = scenario.deps.head
    shutdown = scenario.controller.shutdown
    calls = 0

    async def check_head():
        nonlocal calls
        calls += 1
        if calls == 2:
            shutdown.request_shutdown()
        return await head()

    monkeypatch.setattr(scenario.deps, "head", check_head)
    await generation_iteration(scenario.controller, settings)

    assert shutdown.should_stop
    assert provider.creates == 0


async def test_completed_job_retries_pod_cleanup(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    pod = scenario.controller.pod
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}
    delete = provider.delete

    def unavailable(identifier):
        raise RuntimeError("Fixture provider deletion unavailable")

    monkeypatch.setattr(provider, "delete", unavailable)
    with pytest.raises(RuntimeError, match="deletion unavailable"):
        await generation_iteration(scenario.controller, settings)
    assert scenario.controller.completed_prompts == 2
    assert scenario.controller.pod is pod
    monkeypatch.setattr(provider, "delete", delete)
    await generation_iteration(scenario.controller, settings)

    assert scenario.controller.pod is None
    assert not provider.pods
    assert provider.creates == 1


async def test_old_pod_must_be_deleted_before_starting_the_next_round(scenario, settings, runner, monkeypatch):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    old = scenario.controller.pod
    delete = provider.delete

    def unavailable(pod_id):
        raise RuntimeError("Fixture old pod deletion unavailable")

    monkeypatch.setattr(provider, "delete", unavailable)
    git = scenario.deps.git
    for path, content in list(git.files.items()):
        if path.startswith("rounds/1/"):
            git.files[path.replace("rounds/1/", "rounds/2/")] = content
    git.files["state.json"] = CompetitionState(current_round=2, stage=RoundStage.MINER_GENERATION).model_dump_json()

    def saved(request):
        assert request.url.path == f"/sandbox/rounds/test-2/{scenario.deps.hotkey}/submitted.json"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(saved)) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        with pytest.raises(RuntimeError, match="deletion unavailable"):
            await generation_iteration(scenario.controller, settings)
        assert scenario.controller.pod is old
        assert provider.creates == 1
        monkeypatch.setattr(provider, "delete", delete)
        await generation_iteration(scenario.controller, settings)
    assert scenario.controller.round_number == 2
    assert scenario.controller.pod.id == "pod-2"
    assert provider.events == ["create", "delete", "create"]
    assert provider.pods.keys() == {"pod-2"}


@pytest.mark.parametrize("already_deleted", [False, True])
async def test_new_round_starts_after_old_pod_cleanup_without_reading_old_results(
    scenario, settings, runner, already_deleted
):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    if already_deleted:
        provider.pods.clear()
    git = scenario.deps.git
    for path, content in list(git.files.items()):
        if path.startswith("rounds/1/"):
            git.files[path.replace("rounds/1/", "rounds/2/")] = content
    git.files["state.json"] = CompetitionState(current_round=2, stage=RoundStage.MINER_GENERATION).model_dump_json()

    reads = []

    def saved(request):
        reads.append(request.url.path)
        return httpx.Response(503 if "/test-1/" in request.url.path else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(saved)) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        await generation_iteration(scenario.controller, settings)
    assert scenario.controller.round_number == 2
    assert scenario.controller.pod.id == "pod-2"
    assert provider.pods.keys() == {"pod-2"}
    assert reads == [f"/sandbox/rounds/test-2/{scenario.deps.hotkey}/submitted.json"]


@pytest.mark.parametrize("failed_read", ["ref", "state"])
async def test_git_outage_defers_results_until_the_current_round_is_known(
    scenario, settings, runner, monkeypatch, failed_read
):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    pod = scenario.controller.pod
    reads = scenario.state["index_reads"]
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
    assert scenario.controller.pod is pod
    assert scenario.controller.completed_prompts == 0
    assert scenario.state["index_reads"] == reads
    assert provider.creates == 1
    monkeypatch.setattr(git, "get_ref_sha", original_ref)
    monkeypatch.setattr(git, "get_file", original_file)
    await generation_iteration(scenario.controller, settings)
    assert scenario.controller.completed_prompts == 2
    assert scenario.controller.pod is None
    assert provider.creates == 1
    assert not provider.pods


async def test_price_ceiling_blocks_create(scenario, runner, runpod_errors):
    value, provider = runner
    value.settings.max_hourly_cost = Decimal("0.001")
    with pytest.raises(RuntimeError, match="cost ceiling"):
        await launch(value, scenario)
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
        await launch(value, scenario)

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
            await launch(value, scenario)

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


async def test_create_rechecks_token_validity_after_preflight(scenario, runner, clock, monkeypatch):
    value, provider = runner
    min_validity_seconds = 11400
    credentials = access(clock[0] + min_validity_seconds + 1)
    price = value._price

    def slow_price(config):
        clock[0] += 2
        return price(config)

    monkeypatch.setattr(value, "_price", slow_price)
    with pytest.raises(RuntimeError, match="insufficient validity"):
        await value.start(1, scenario.deps.hotkey, credentials, min_validity_seconds=min_validity_seconds)
    assert provider.creates == 0


@pytest.mark.parametrize("go_memory_mib", [14 * 1024, 16 * 1024])
async def test_price_check_requires_ram_for_the_prover_and_worker(scenario, runner, runpod_errors, go_memory_mib):
    value, provider = runner
    value.settings.go_memory_mib = go_memory_mib
    if go_memory_mib == 14 * 1024:
        await launch(value, scenario)
        assert provider.creates == 1
        assert runpod_errors == []
    else:
        with pytest.raises(RuntimeError, match="RAM"):
            await launch(value, scenario)
        assert provider.creates == 0
        assert runpod_errors == ["RunPod CPU cpu3c offers 16384 MiB RAM but the worker needs 18432 MiB"]


@pytest.mark.parametrize("phase", ["EXITED", "FAILED", None])
async def test_broken_pod_is_replaced_after_a_stall_and_preserves_witness_progress(
    scenario, settings, runner, clock, phase
):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    pod = scenario.controller.pod
    scenario.state["saved"] = {"chair": {"failure_reason": "generation_failed"}}
    if phase is None:
        provider.pods.clear()
    else:
        provider.pods[pod.id] = Pod(pod.id, pod.name, pod.image, phase, {})
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1
    assert provider.lookups == []
    clock[0] += settings.stall_replacement_seconds
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]
    assert provider.lookups == [pod.id]
    assert scenario.controller.pod.id == "pod-2"
    assert scenario.controller.completed_prompts == 1
    assert scenario.state["saved"] == {"chair": {"failure_reason": "generation_failed"}}
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 2


async def test_create_rejects_a_different_hotkey(scenario, settings, runner):
    value, provider = runner
    wallet = Wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey, path=str(settings.wallet_path))
    wallet.set_hotkey(Keypair.create_from_uri("//Bob"), encrypt=False, overwrite=True)
    with pytest.raises(ValueError, match="Operator hotkey changed"):
        await launch(value, scenario)
    assert provider.creates == 0


async def test_start_request_passes_the_competition_location_and_round(scenario, settings, runner):
    value, provider = runner
    settings.github_branch = "witness-test"
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    config = runpod.Config(**json.loads(provider.bodies[0]["env"]["LEADER_JOB_CONFIG"]))
    assert config.competition_repository == settings.github_repo
    assert config.competition_branch == settings.github_branch
    assert config.round_number == 1
    assert config.generation_image == settings.generation_image
    assert provider.bodies[0]["image"] == config.generation_image
    assert config.hotkey == scenario.deps.hotkey
    assert config.account_id == "abc123"
    assert config.witness_url == settings.witness_url
    assert (config.workers, config.parallel_calls, config.parallel_preparations) == (24, 32, 4)
    assert (config.go_cpus, config.go_memory_mib) == (8, 2048)
    assert provider.bodies[0]["name"] == scenario.controller.pod.name


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
    assert scenario.controller.pod.id == "pod-1"


async def test_closed_round_disposes_running_attempt_without_replacement(runner, scenario, settings, clock):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    scenario.state["block"] = 100
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert not provider.pods
    assert provider.creates == 1
    assert scenario.controller.pod is None


@pytest.mark.parametrize("replacement_seconds", [None, 7200], ids=["one-hour-default", "two-hours-configured"])
async def test_no_progress_warns_at_twenty_minutes_and_uses_the_replacement_threshold(
    runner, scenario, settings, clock, discord, replacement_seconds
):
    if replacement_seconds is not None:
        settings.stall_replacement_seconds = replacement_seconds
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    previous = scenario.controller.pod
    clock[0] += 1199
    await generation_iteration(scenario.controller, settings)
    assert discord.messages == [] and provider.deleted == []
    clock[0] += 1
    await generation_iteration(scenario.controller, settings)
    assert scenario.controller.pod is previous
    assert provider.creates == 1 and provider.deleted == []
    assert len(discord.messages) == 1
    clock[0] += (replacement_seconds or 3600) - 1201
    await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and provider.deleted == []
    clock[0] += 1
    await generation_iteration(scenario.controller, settings)
    assert provider.events == ["create", "delete", "create"]
    assert scenario.controller.pod is not previous
    assert len(discord.messages) == 1


async def test_saved_progress_keeps_a_running_generation_pod_and_logs_counts(
    runner, scenario, settings, clock, discord
):
    from loguru import logger

    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    previous = scenario.controller.pod
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
        assert scenario.controller.pod is previous
        assert len(discord.messages) == 1
        clock[0] += 1200
        scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
        await generation_iteration(scenario.controller, settings)
        assert any("2/2" in message and "+1" in message for message in messages)
    finally:
        logger.remove(sink)
    assert scenario.controller.completed_prompts == 2
    assert scenario.controller.pod is None
    assert provider.creates == 1 and not provider.pods


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
    assert not scenario.controller.progress_warning_sent
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
    progress_at = scenario.controller.last_progress_at
    clock[0] += settings.stall_replacement_seconds
    scenario.state["saved"] = {}
    with pytest.raises(ValueError, match="count regressed"):
        await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and provider.deleted == []
    assert scenario.controller.last_progress_at == progress_at
    assert scenario.controller.completed_prompts == 1


async def test_closed_round_cleans_up_even_when_progress_is_unavailable(runner, scenario, settings):
    value, provider = runner
    scenario.deps.runner = value
    await generation_iteration(scenario.controller, settings)
    scenario.state["block"] = 100

    def unavailable(request):
        pytest.fail("A closed round does not read the index")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        scenario.deps.saved = SavedResults(http, settings.cdn_url)
        await generation_iteration(scenario.controller, settings)
    assert provider.creates == 1 and not provider.pods
    assert scenario.controller.pod is None


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
    assert scenario.controller.completed_prompts == 1
    assert scenario.controller.pod is not None
    assert provider.creates == 1 and provider.deleted == []
    assert discord.messages == []
    scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
    clock[0] += 1200
    await generation_iteration(scenario.controller, settings)
    assert scenario.controller.completed_prompts == 2
    assert scenario.controller.pod is None
    assert provider.creates == 1 and not provider.pods
