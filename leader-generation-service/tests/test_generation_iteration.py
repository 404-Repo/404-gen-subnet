import asyncio
import json
import time
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from conftest import access
from pydantic import ValidationError
from subnet_common.competition.state import CompetitionState, RoundStage

from leader_generation_service.credentials import CredentialManager
from leader_generation_service.generation_iteration import generation_iteration, maintain_credentials
from leader_generation_service.settings import Settings
from leader_generation_service.store import JobStore, exclusive_lock


@pytest.mark.parametrize("stage", [s for s in RoundStage if s != RoundStage.MINER_GENERATION])
async def test_idle_stage_never_starts_work(scenario, settings, stage):
    scenario.deps.git.files["state.json"] = CompetitionState(current_round=1, stage=stage).model_dump_json()
    await generation_iteration(scenario.controller, settings)
    assert scenario.state["input_reads"] == 0
    assert scenario.deps.runner.started == []
    assert scenario.deps.store.jobs() == []


async def test_new_round_freezes_inputs_and_next_iteration_keeps_the_pod(scenario, settings):
    references = []
    original = scenario.deps.git.get_file

    async def get_file(path, ref):
        references.append(ref)
        return await original(path, ref)

    scenario.deps.git.get_file = get_file
    await generation_iteration(scenario.controller, settings)
    job = JobStore(settings.state_directory).load(1)
    assert job.snapshot.leader.repo == "test/incumbent"
    assert job.snapshot.leader.commit == "b" * 40
    assert job.snapshot.inputs == scenario.inputs
    assert job.snapshot.hotkey == scenario.deps.hotkey
    assert (job.snapshot.workers, job.snapshot.parallel_calls, job.snapshot.parallel_preparations) == (24, 32, 4)
    assert (job.snapshot.go_cpus, job.snapshot.go_memory_mib) == (8, 2048)
    assert set(references) == {"a" * 40}
    await generation_iteration(scenario.controller, settings)
    assert scenario.state["input_reads"] == 1
    assert scenario.deps.store.load(1).snapshot == job.snapshot
    assert scenario.deps.store.load(1).allocation.id == job.allocation.id


@pytest.mark.parametrize("problem", ["seed", "prompts", "deadline", "saving"])
async def test_witness_mismatch_stops_before_generation(scenario, settings, problem):
    if problem == "seed":
        scenario.deps.git.files["rounds/1/seed.json"] = '{"seed":43}'
    elif problem == "prompts":
        scenario.deps.git.files["rounds/1/prompts.txt"] = "https://images.test/other.png"
    else:
        schedule = json.loads(scenario.deps.git.files["rounds/1/schedule.json"])
        schedule["generation_deadline_block" if problem == "deadline" else "witness_save_submissions"] = (
            99 if problem == "deadline" else False
        )
        scenario.deps.git.files["rounds/1/schedule.json"] = json.dumps(schedule)
    with pytest.raises(ValueError):
        await generation_iteration(scenario.controller, settings)
    assert scenario.deps.runner.ensured == []


async def test_deadline_closes_before_any_allocation(scenario, settings):
    scenario.state["block"] = 100
    await generation_iteration(scenario.controller, settings)
    assert scenario.state["input_reads"] == 0
    assert scenario.deps.runner.started == []


async def test_stage_is_rechecked_after_runtime_preparation(scenario, settings):
    def close():
        scenario.deps.git.files["state.json"] = CompetitionState(
            current_round=1, stage=RoundStage.COLLECTING
        ).model_dump_json()
        scenario.deps.git.ref_sha = "c" * 40

    scenario.deps.runner.on_ensure = close
    await generation_iteration(scenario.controller, settings)
    assert len(scenario.deps.runner.ensured) == 1
    assert scenario.deps.runner.started == []


async def test_shutdown_during_preparation_does_not_start_generation(scenario, settings):
    shutdown = scenario.controller.shutdown
    scenario.deps.runner.on_ensure = shutdown.request_shutdown
    await generation_iteration(scenario.controller, settings)
    assert scenario.deps.runner.started == []
    assert scenario.deps.store.load(1).allocation.phase == "active"


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
    manager = CredentialManager(scenario.deps.store.directory, "fixture-codex")
    value = await manager.current(min_validity_seconds=min_validity_seconds)
    assert value.expires_at > time.time() + min_validity_seconds
    assert calls == [["fixture-codex", "--version"], "refresh"]


async def test_generation_finishes_with_a_complete_witness_index(scenario, settings):
    await generation_iteration(scenario.controller, settings)
    journal = settings.state_directory / "rounds/1.json"
    legacy_job = json.loads(journal.read_text())
    legacy_job["published"] = False
    journal.write_text(json.dumps(legacy_job))

    scenario.state["saved"] = {"chair": {"js": "chair.js", "views": "chair", "size": 12}}
    await generation_iteration(scenario.controller, settings)
    assert scenario.deps.store.load(1).completed_prompts == 1
    assert not scenario.deps.store.load(1).completed
    scenario.state["saved"]["lamp"] = {"failure_reason": "generation_failed"}
    await generation_iteration(scenario.controller, settings)

    job = scenario.deps.store.load(1)
    assert job.completed and job.allocation.phase == "terminated"
    assert len(scenario.deps.runner.started) == 1


async def test_saved_manual_results_finish_the_job_before_cpu_allocation(scenario, settings):
    scenario.state["saved"] = {name: {"failure_reason": "generation_failed"} for name in scenario.images}

    await generation_iteration(scenario.controller, settings)

    job = scenario.deps.store.load(1)
    assert job.completed and job.allocation is None
    assert scenario.deps.runner.started == []


@pytest.mark.parametrize("hotkey", [None, "5" * 48])
async def test_generation_requires_the_scheduled_operator_wallet(scenario, settings, hotkey):
    files = scenario.deps.git.files
    path = "rounds/1/schedule.json"
    schedule = json.loads(files[path])
    schedule["leader_generation_hotkey"] = hotkey
    files[path] = json.dumps(schedule)

    if hotkey is None:
        await generation_iteration(scenario.controller, settings)
    else:
        with pytest.raises(ValueError, match="differs from the operator wallet"):
            await generation_iteration(scenario.controller, settings)

    assert scenario.deps.store.jobs() == []
    assert scenario.state["input_reads"] == 0


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


async def test_outer_loop_waits_the_configured_interval_after_every_failure(scenario, settings, monkeypatch):
    spec = spec_from_file_location("leader_service_main", Path(__file__).parents[1] / "main.py")
    assert spec is not None and spec.loader is not None
    service = module_from_spec(spec)
    spec.loader.exec_module(service)

    settings.progress_interval_seconds = 1800
    shutdown = scenario.controller.shutdown
    original_wait = shutdown.wait
    cycles, waits = [], []

    @asynccontextmanager
    async def dependencies(_, service_shutdown):
        assert service_shutdown is shutdown
        yield scenario.deps

    async def cycle(*args):
        cycles.append(1)
        raise RuntimeError("Fixture iteration failure")

    async def maintain(*args):
        await original_wait()

    async def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 3:
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
    await service.main()
    assert len(cycles) == 3
    assert waits == [1800, 1800, 1800]
