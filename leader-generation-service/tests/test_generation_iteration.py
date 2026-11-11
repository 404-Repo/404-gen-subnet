import asyncio
import json
import time
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from conftest import access
from loguru import logger
from pod_gateway.runpod import CreateUnknown
from pydantic import SecretStr, ValidationError
from subnet_common.competition.state import CompetitionState, RoundStage

from leader_generation_service.credentials import CredentialManager
from leader_generation_service.generation_iteration import generation_iteration, maintain_credentials
from leader_generation_service.settings import Settings
from leader_generation_service.store import exclusive_lock


@pytest.fixture
def iteration_logs():
    messages = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]), filter=generation_iteration.__module__
    )
    try:
        yield messages
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("stage", [s for s in RoundStage if s != RoundStage.MINER_GENERATION])
async def test_idle_stage_never_starts_work(scenario, settings, stage, iteration_logs):
    scenario.deps.git.files["state.json"] = CompetitionState(current_round=1, stage=stage).model_dump_json()
    await generation_iteration(scenario.controller, settings)
    assert f"Round 1 skipped because stage is {stage.value}" in iteration_logs
    assert scenario.deps.runner.started == []
    assert scenario.controller.pod is None


async def test_new_round_starts_without_worker_inputs_and_keeps_the_pod(scenario, settings, iteration_logs):
    del scenario.deps.git.files["leader.json"]
    del scenario.deps.git.files["rounds/1/seed.json"]
    references = []
    original = scenario.deps.git.get_file

    async def get_file(path, ref):
        references.append(ref)
        return await original(path, ref)

    scenario.deps.git.get_file = get_file
    await generation_iteration(scenario.controller, settings)
    assert iteration_logs[-4:] == [
        "Competition 1 at aaaaaaaaaa: miner_generation",
        "Round 1 loading competition settings",
        "Round 1 checking the generation deadline on Finney",
        "Round 1 checking saved witness results",
    ]
    pod = scenario.controller.pod
    assert pod is not None
    assert set(references) == {"a" * 40}
    await generation_iteration(scenario.controller, settings)
    assert scenario.deps.runner.started == [1]
    assert scenario.controller.pod is pod
    assert scenario.deps.runner.terminated == []


async def test_generation_requires_saving_enabled_in_git(scenario, settings):
    files = scenario.deps.git.files
    schedule = json.loads(files["rounds/1/schedule.json"])
    schedule["witness_save_submissions"] = False
    files["rounds/1/schedule.json"] = json.dumps(schedule)
    with pytest.raises(ValueError, match="requires witness result saving"):
        await generation_iteration(scenario.controller, settings)
    assert scenario.deps.runner.started == []


async def test_deadline_closes_before_any_allocation(scenario, settings, iteration_logs):
    scenario.state["block"] = 100
    await generation_iteration(scenario.controller, settings)
    assert "Round 1 skipped because the generation deadline has been reached" in iteration_logs
    assert scenario.deps.runner.started == []


@pytest.mark.parametrize("change", ["stage", "hotkey"])
async def test_generation_eligibility_is_rechecked_after_loading_credentials(scenario, settings, change):
    load = scenario.deps.credentials.load

    def load_credentials(min_validity_seconds):
        files = scenario.deps.git.files
        if change == "stage":
            files["state.json"] = CompetitionState(current_round=1, stage=RoundStage.COLLECTING).model_dump_json()
        else:
            config = json.loads(files["config.json"])
            del config["leader_generation_hotkey"]
            files["config.json"] = json.dumps(config)
        scenario.deps.git.ref_sha = "c" * 40
        return load(min_validity_seconds)

    scenario.deps.credentials.load = load_credentials
    await generation_iteration(scenario.controller, settings)
    assert scenario.deps.runner.started == []


async def test_shutdown_during_credential_load_does_not_start_generation(scenario, settings):
    load = scenario.deps.credentials.load

    def load_credentials(min_validity_seconds):
        scenario.controller.shutdown.request_shutdown()
        return load(min_validity_seconds)

    scenario.deps.credentials.load = load_credentials
    await generation_iteration(scenario.controller, settings)
    assert scenario.deps.runner.started == []


@pytest.mark.parametrize("min_validity_seconds", [600, 11400])
async def test_service_refreshes_proactively_using_pinned_cli_only(scenario, monkeypatch, min_validity_seconds):
    from leader_generation_service import credentials

    current = [access(time.time() + min_validity_seconds - 10)]
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return b"codex-cli 0.167.0\n"

    def refresh(home, executable):
        current[0] = access(time.time() + min_validity_seconds + 3600)
        calls.append("refresh")

    monkeypatch.setattr(credentials, "execute", execute)
    monkeypatch.setattr(credentials, "subscription_credentials", lambda *args: (current[0].access_token, "abc123"))
    monkeypatch.setattr(credentials, "_refresh_codex", refresh)
    manager = CredentialManager(scenario.deps.credentials.home, "fixture-codex")
    value = await manager.current(min_validity_seconds=min_validity_seconds)
    assert value.expires_at > time.time() + min_validity_seconds
    assert calls == [["fixture-codex", "--version"], "refresh"]


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("command", "Codex CLI version command failed (RuntimeError)"),
        ("version", "Codex CLI version mismatch. Expected codex-cli 0.167.0."),
        ("refresh", "Codex subscription load or refresh failed (RuntimeError)"),
        ("validity", "Codex subscription refresh returned a token with insufficient validity"),
    ],
)
async def test_subscription_failure_logs_the_failed_check(scenario, monkeypatch, failure, expected):
    from leader_generation_service import credentials

    def execute(*args, **kwargs):
        if failure == "command":
            raise RuntimeError("Fixture command output")
        return b"codex-cli 0.167.1\n" if failure == "version" else b"codex-cli 0.167.0\n"

    def read_login(*args):
        assert failure in ("refresh", "validity")
        value = access(time.time() + 500)
        return value.access_token, value.account_id

    def refresh(*args):
        if failure == "refresh":
            raise RuntimeError("Fixture refresh response")

    monkeypatch.setattr(credentials, "execute", execute)
    monkeypatch.setattr(credentials, "subscription_credentials", read_login)
    monkeypatch.setattr(credentials, "_refresh_codex", refresh)
    manager = CredentialManager(scenario.deps.credentials.home, "fixture-codex")
    errors = []
    sink = logger.add(
        lambda message: errors.append(message.record["message"]), level="ERROR", filter=credentials.__name__
    )
    try:
        with pytest.raises(RuntimeError):
            await manager.current()
    finally:
        logger.remove(sink)
    assert errors == [expected]


async def test_generation_finishes_with_a_complete_witness_index(scenario, settings):
    await generation_iteration(scenario.controller, settings)
    pod = scenario.controller.pod
    scenario.state["saved"] = {"chair": {"js": "chair.js", "views": "chair", "size": 12}}
    await generation_iteration(scenario.controller, settings)
    assert scenario.controller.completed_prompts == 1
    assert scenario.controller.pod is pod
    scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
    await generation_iteration(scenario.controller, settings)

    assert scenario.controller.completed_prompts == 2
    assert scenario.controller.pod is None
    assert scenario.deps.runner.terminated == [pod]
    assert scenario.deps.runner.started == [1]


async def test_saved_manual_results_finish_the_job_before_cpu_allocation(scenario, settings):
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}

    await generation_iteration(scenario.controller, settings)

    assert scenario.controller.completed_prompts == 2
    assert scenario.controller.pod is None
    assert scenario.deps.runner.started == []


@pytest.mark.parametrize("hotkey", [None, "5" * 48])
async def test_generation_requires_the_configured_operator_wallet(scenario, settings, hotkey, iteration_logs):
    files = scenario.deps.git.files
    config = json.loads(files["config.json"])
    config["leader_generation_hotkey"] = hotkey
    files["config.json"] = json.dumps(config)
    schedule = json.loads(files["rounds/1/schedule.json"])
    schedule["leader_generation_hotkey"] = scenario.deps.hotkey
    files["rounds/1/schedule.json"] = json.dumps(schedule)

    if hotkey is None:
        await generation_iteration(scenario.controller, settings)
        assert "Round 1 skipped because config.json has no leader_generation_hotkey" in iteration_logs
    else:
        with pytest.raises(ValueError, match="differs from the operator wallet"):
            await generation_iteration(scenario.controller, settings)
        assert "Round 1 configured hotkey differs from the operator wallet" in iteration_logs

    assert scenario.deps.runner.started == []


async def test_credentials_renew_while_idle_and_serialize_callers(tmp_path):
    clock = [time.time()]
    loads = []

    def load(_min_validity_seconds):
        loads.append(clock[0])
        return access(clock[0] + 800)

    manager = CredentialManager(tmp_path, "fixture", load=load, clock=lambda: clock[0])
    first = await asyncio.gather(*(manager.current() for _ in range(8)))
    assert len(loads) == 1
    clock[0] += 300
    second = await manager.current()
    assert second.access_token != first[0].access_token
    assert len(loads) == 2


async def test_maintenance_renews_without_a_round_or_pod(scenario, settings, monkeypatch):
    shutdown = scenario.controller.shutdown
    calls = []
    original = scenario.deps.credentials.load

    def load(min_validity_seconds):
        calls.append("refresh")
        return original(min_validity_seconds)

    scenario.deps.credentials.load = load

    async def stop(timeout=None):
        shutdown.request_shutdown()
        return True

    monkeypatch.setattr(shutdown, "wait", stop)
    await maintain_credentials(scenario.deps, settings, shutdown)
    assert calls == ["refresh"]
    assert scenario.deps.runner.started == []


def test_subscription_ownership_lock_is_exclusive(tmp_path):
    with exclusive_lock(tmp_path / "login.lock"), pytest.raises(RuntimeError, match="Another service"):
        with exclusive_lock(tmp_path / "login.lock"):
            pytest.fail("Second refresh owner acquired the lock")


@pytest.mark.parametrize("interval", [1199, 1200, 1800])
def test_progress_interval_has_a_twenty_minute_minimum(settings, interval):
    values = settings.model_dump(by_alias=True)
    values["progress_interval_seconds"] = interval
    if interval < 1200:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)
    else:
        assert Settings(_env_file=None, **values).progress_interval_seconds == interval


@pytest.mark.parametrize("unknown_create", [False, True])
async def test_main_cleans_up_at_startup_and_waits_after_failures(scenario, settings, monkeypatch, unknown_create):
    spec = spec_from_file_location("leader_service_main", Path(__file__).parents[1] / "main.py")
    assert spec is not None and spec.loader is not None
    service = module_from_spec(spec)
    spec.loader.exec_module(service)

    settings.progress_interval_seconds = 1800
    settings.github_branch = "witness-test"
    settings.cdn_url = "https://cdn.test/fixture-private-path"
    settings.discord_webhook_url = SecretStr("https://discord.test/fixture-webhook")
    shutdown = scenario.controller.shutdown
    original_wait = shutdown.wait
    waits_at_cycle_start, waits = [], []

    @asynccontextmanager
    async def dependencies(_, service_shutdown):
        assert service_shutdown is shutdown
        yield scenario.deps

    async def cycle(controller, service_settings):
        assert scenario.deps.runner.startup_cleanups == 1
        waits_at_cycle_start.append(len(waits))
        if unknown_create:
            raise CreateUnknown("Fixture create reply unavailable")
        if len(waits_at_cycle_start) == 1:
            await generation_iteration(controller, service_settings)
            return
        if len(waits_at_cycle_start) == 2:
            request = httpx.Request("GET", "https://github.test/config.json?token=fixture")
            httpx.Response(503, request=request).raise_for_status()
        raise RuntimeError("Fixture iteration failure")

    async def maintain(*args):
        await original_wait()

    async def wait(timeout=None):
        waits.append(timeout)
        if not unknown_create and len(waits) == 3:
            shutdown.request_shutdown()
        return shutdown.should_stop

    monkeypatch.setattr(shutdown, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(shutdown, "remove_signal_handlers", lambda: None)
    monkeypatch.setattr(shutdown, "wait", wait)
    monkeypatch.setattr(service, "Settings", lambda: settings)
    monkeypatch.setattr(service, "GracefulShutdown", lambda: shutdown)
    monkeypatch.setattr(service, "setup_logging", lambda _: None)
    monkeypatch.setattr(service, "create_dependencies", dependencies)
    monkeypatch.setattr(service, "generation_iteration", cycle)
    monkeypatch.setattr(service, "maintain_credentials", maintain)
    messages = []
    sink = logger.add(lambda message: messages.append(message.record["message"]), level="INFO", filter=service.__name__)
    try:
        await service.main()
    finally:
        logger.remove(sink)
    assert waits_at_cycle_start == ([0] if unknown_create else [0, 1, 2])
    assert scenario.deps.runner.started == ([] if unknown_create else [1])
    assert waits == [1800] * len(waits_at_cycle_start)
    assert scenario.deps.runner.startup_cleanups == 1
    failure_logs = (
        ["Restarting in 0:30:00 for cleanup after an unconfirmed pod create"]
        if unknown_create
        else [
            "Leader generation cycle failed at github.test (HTTP 503). Retrying in 0:30:00.",
            "Leader generation cycle failed (RuntimeError). Retrying in 0:30:00.",
        ]
    )
    assert messages == [
        "Leader generation service started. GITHUB_REPO=test/competition GITHUB_BRANCH=witness-test "
        f"WITNESS_URL=https://witness.test CDN host=cdn.test STATE_DIRECTORY={settings.state_directory} "
        f"operator hotkey={scenario.deps.hotkey} generation image digest=sha256:{'d' * 64}",
        "Startup cleanup complete",
        *failure_logs,
        "Leader generation service stopped gracefully",
    ]
