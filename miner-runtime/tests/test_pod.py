"""Offline execution of Pod startup, credential delivery and witness-saved resume."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from miner_runtime import pod

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External I/O is forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(pod, "execute", forbidden)


@pytest.fixture
def config():
    return pod.Config(generation_image="registry.test/leader@sha256:" + "d" * 64,
                      source_repository="https://github.com/fixture/miner.git", source_commit="b" * 40,
                      prompts={"chair": hashlib.sha256(b"first").hexdigest(),
                               "table": hashlib.sha256(b"second").hexdigest()}, seed=42,
                      hotkey="5" + "A" * 47, account_id="abc123", credentials_url="https://leader.test/jobs/3")


def context(config):
    inputs = {"repository": "fixture/competition", "branch": "test", "revision": "c" * 40,
              "round": "fixture-3", "number": 3, "seed": config.seed, "storage_prefix": "",
              "generation_deadline_block": None, "save_submissions": True,
              "prompts": [{"id": key, "sha256": value, "source_url": f"https://images.test/{key}.png",
                           "image_key": f"competition-inputs/v1/{value}"} for key, value in config.prompts.items()]}
    return {"round_inputs": inputs, "saving_enabled": True,
            "prompts": [{**row, "url": row["source_url"]} for row in inputs["prompts"]]}


def token(account="abc123", expiry=None):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry or time.time() + 3600}).encode()).decode().rstrip("=")
    return {"access_token": "fixture." + payload + "." + "s" * 64, "account_id": account}


def test_request_starts_worker_and_each_attempt_gets_a_new_pod(config):
    first = pod.pod_body(config, "fixture-job-token")
    second = pod.pod_body(config, "fixture-job-token")
    assert first["name"] != second["name"]
    assert first["cpu"] == {"id": "cpu3c", "vcpuCount": 8}
    assert first["image"] == config.generation_image
    assert pod.Config(**json.loads(first["env"]["LEADER_JOB_CONFIG"])) == config
    assert pod.pod_body(config, "token", name="leader-runner-fixed")["name"] == "leader-runner-fixed"


def test_worker_exception_logs_omit_local_values(config, tmp_path):
    script = tmp_path / "log_failure.py"
    script.write_text(
        """from loguru import logger

def fail(value):
    raise RuntimeError("fixture failure")

def run():
    value = "fixture-local-value"
    try:
        fail(value)
    except RuntimeError:
        logger.exception("Prompt failed")

run()
"""
    )
    environment = {**os.environ, "LOGURU_DIAGNOSE": "YES", **pod.pod_body(config, "fixture-job-token")["env"]}
    result = subprocess.run(
        [sys.executable, str(script)], env=environment, capture_output=True, text=True, check=True, timeout=5,
    )
    assert "RuntimeError: fixture failure" in result.stderr
    assert "fixture-local-value" not in result.stderr




def test_wire_fingerprint_matches_shared_round_model(config):
    from subnet_common.witness.rounds import RoundInputs
    inputs = context(config)["round_inputs"]
    assert pod.fingerprint(inputs) == RoundInputs.model_validate_json(json.dumps(inputs)).fingerprint


@pytest.mark.parametrize("mismatch", [None, "source", "development", "library", "parameters"])
def test_worker_checks_prebuilt_image_source_and_proof_integrity(tmp_path, monkeypatch, mismatch):
    import witness_client

    library = tmp_path / "libwitnessproof.so"
    library.write_bytes(b"fixture native library")
    metadata = {"runtime_commit": "c" * 40, "purpose": "release", "library_sha256": pod.digest(library)}
    if mismatch == "source":
        metadata["runtime_commit"] = "invalid"
    elif mismatch == "development":
        metadata["purpose"] = "development"
    elif mismatch == "library":
        metadata["library_sha256"] = "0" * 64
    manifest = tmp_path / "provenance.json"
    manifest.write_text(json.dumps(metadata))
    parameters = tmp_path / "params"
    parameters.mkdir()
    for name in pod.PARAMETERS:
        (parameters / name).write_bytes(name.encode())
    monkeypatch.setattr(pod, "PARAMETERS", {path.name: pod.digest(path) for path in parameters.iterdir()})
    monkeypatch.setattr(pod, "IMAGE_MANIFEST", manifest)
    monkeypatch.setattr(pod, "IMAGE_PARAMETERS", parameters)
    monkeypatch.setattr(witness_client, "__file__", str(tmp_path / "__init__.py"))
    if mismatch == "parameters":
        (parameters / "verifying.bin").write_bytes(b"changed")
    if mismatch is None:
        assert pod.verify_image() == "c" * 40
    else:
        with pytest.raises(RuntimeError, match="invalid|differs|differ|development"):
            pod.verify_image()


def test_image_entrypoint_runs_the_worker_with_private_credentials_and_prover_settings(
        config, tmp_path, monkeypatch, capsys):
    events = []
    for key in pod.runtime_environment(config):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.setenv("LEADER_JOB_CONFIG", json.dumps(asdict(config)))
    monkeypatch.setenv("LEADER_JOB_TOKEN", "fixture-job-token")
    monkeypatch.setattr(pod, "REMOTE", tmp_path)

    def image():
        events.append(("image", "c" * 40))
        return "c" * 40

    monkeypatch.setattr(pod, "verify_image", image)
    monkeypatch.setattr(pod, "check_hardware", lambda value: events.append(("hardware", value)))
    monkeypatch.setattr(pod, "fetch_source", lambda value, directory: events.append(("source", directory)))

    async def worker(value, directory, token):
        assert value == config
        assert directory == tmp_path
        assert token == "fixture-job-token"
        assert os.environ.get("LEADER_JOB_TOKEN") is None
        assert {key: os.environ[key] for key in pod.runtime_environment(config)} == pod.runtime_environment(config)
        events.append(("worker", directory))

    monkeypatch.setattr(pod, "run_job", worker)
    previous = os.umask(0o077)
    try:
        pod.main()
    finally:
        os.umask(previous)
    assert [event[0] for event in events] == ["image", "hardware", "source", "worker"]
    assert "Generation image source commit: " + "c" * 40 in capsys.readouterr().out


@pytest.mark.parametrize("change", ["seed", "prompts", "saving", "scope"])
def test_job_context_is_validated_before_work(config, change):
    data = context(config)
    config = replace(config, round_inputs_fingerprint=pod.fingerprint(data["round_inputs"]))
    if change == "seed":
        data["round_inputs"]["seed"] += 1
    elif change == "prompts":
        data["round_inputs"]["prompts"] = []
    elif change == "scope":
        data["round_inputs"]["round"] = "different-3"
    else:
        data["saving_enabled"] = False
    with pytest.raises(ValueError):
        pod.check_context(config, data)


def test_access_credentials_rotate_without_replacing_identity(config, tmp_path):
    hotkey = tmp_path / "private/wallets/leader/hotkeys/default"
    hotkey.parent.mkdir(parents=True)
    hotkey.write_bytes(b"fixture identity")
    first = token()
    pod.install_credentials(config, tmp_path, first)
    second = token(expiry=time.time() + 7200)
    pod.install_credentials(config, tmp_path, second)
    credential_file = tmp_path / "private/subscription.json"
    assert json.loads(credential_file.read_bytes()) == second
    assert credential_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="account differs"):
        pod.install_credentials(config, tmp_path, token(account="def456"))
    assert json.loads(credential_file.read_bytes()) == second
    assert hotkey.read_bytes() == b"fixture identity"


def test_identity_is_checked_before_installing_or_replacing_it(config, tmp_path):
    from bittensor_wallet import Keypair, Wallet

    source = tmp_path / "identity-source"
    wallet = Wallet(name="leader", hotkey="default", path=str(source))
    key = Keypair.create_from_mnemonic(Keypair.generate_mnemonic(12))
    wallet.set_hotkey(key, encrypt=False, overwrite=True)
    path = source / "leader/hotkeys/default"
    config = replace(config, hotkey=key.ss58_address)
    target = tmp_path / "pod"
    target.mkdir()
    pod.install_identity(config, target, base64.b64encode(path.read_bytes()).decode())
    installed = Wallet(name="leader", hotkey="default", path=str(target / "private/wallets"))
    assert installed.hotkey.ss58_address == key.ss58_address
    other = Keypair.create_from_mnemonic(Keypair.generate_mnemonic(12))
    wallet.set_hotkey(other, encrypt=False, overwrite=True)
    with pytest.raises(ValueError, match="hotkey differs"):
        pod.install_identity(config, target, base64.b64encode(path.read_bytes()).decode())
    assert Wallet(name="leader", hotkey="default", path=str(target / "private/wallets")).hotkey.ss58_address == key.ss58_address


@pytest.fixture
def generation(config, monkeypatch):
    import bittensor_wallet
    import witness_client
    from miner_runtime import batch

    state = {"rows": {}, "saved": [], "reads": [], "failure": "table", "clients": 0,
             "contexts": 0, "changed_context": None, "context": context(config)}
    images = {"chair": b"first", "table": b"second"}
    key = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=config.hotkey))
    monkeypatch.setattr(bittensor_wallet, "Wallet", lambda **kwargs: key)
    monkeypatch.setattr(batch, "Wallet", lambda **kwargs: key)

    class Witness:
        def __init__(self, *args, **kwargs):
            state["clients"] += 1
            self.index = state["clients"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def replace_pod(self):
            pass

        def renderer_transport(self):
            state["contexts"] += 1
            data = copy.deepcopy(state["context"])
            if state["changed_context"] is not None and state["contexts"] >= state["changed_context"]:
                data["round_inputs"]["round"] = "different-4"
            return httpx.MockTransport(lambda request: httpx.Response(200, json=data))

        async def get_submitted(self):
            return state["rows"]

        async def submit(self, image, result):
            prompt = next(key for key, value in images.items() if image == value)
            if prompt == state["failure"]:
                raise RuntimeError("Fixture save interrupted")
            state["saved"].append(prompt)
            state["rows"][prompt] = {"js": result, "views": "saved-views"}

    async def request(transport, request):
        prompt = Path(request.url.path).stem
        state["reads"].append(prompt)
        return httpx.Response(200, content=images[prompt])

    monkeypatch.setattr(witness_client, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(batch, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", request)
    async def once(config, directory):
        await pod.generate_pass(config, Witness(), pod.load_miner(directory / "source").generate)

    state["once"] = once
    return state


def prepare_worker(directory):
    (directory / "source").mkdir(parents=True)
    (directory / "source/main.py").write_text("async def generate(image, seed, witness): return image.decode()\n")


def test_new_pod_resumes_saved_generations_after_previous_disk_is_lost(config, generation, tmp_path):
    old = tmp_path / "old-pod"
    prepare_worker(old)
    with pytest.raises(RuntimeError, match="prompts remain"):
        asyncio.run(generation["once"](config, old))
    assert generation["saved"] == ["chair"]
    shutil.rmtree(old)
    fresh = tmp_path / "new-pod"
    prepare_worker(fresh)
    generation["reads"].clear()
    generation["failure"] = None
    asyncio.run(generation["once"](config, fresh))
    assert generation["reads"] == ["table"]
    assert generation["saved"] == ["chair", "table"]
    assert set(generation["rows"]) == set(config.prompts)


@pytest.mark.parametrize("phase, check", [("preflight", 1), ("retry", 4), ("final", 4)])
def test_frozen_scope_is_checked_before_each_saved_lookup(config, generation, tmp_path, phase, check):
    config = replace(config, round_inputs_fingerprint=pod.fingerprint(generation["context"]["round_inputs"]))
    prepare_worker(tmp_path)
    generation["changed_context"] = check
    if phase == "final":
        generation["failure"] = None
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(generation["once"](config, tmp_path))
    assert generation["contexts"] >= check
    assert generation["clients"] == 1


def test_credentials_are_delivered_during_foreground_generation(config, tmp_path, monkeypatch):
    values = [token(), token(expiry=time.time() + 7200)]
    calls = []

    async def request(transport, request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer fixture-job-token"
        if request.url.path.endswith("/identity"):
            return httpx.Response(200, json={"hotkey": "fixture-hotkey"})
        return httpx.Response(200, json=values[min(calls.count(request.url.path) - 1, 1)])

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", request)
    monkeypatch.setattr(pod, "install_identity", lambda *args: None)

    async def generate(*args):
        while calls.count("/jobs/3/credentials") < 2:
            await asyncio.sleep(0)

    original_sleep = asyncio.sleep

    async def sleep(delay):
        await original_sleep(0)

    async def observe(*args, **kwargs):
        await generate()
        assert json.loads((tmp_path / "private/subscription.json").read_bytes()) == values[1]

    monkeypatch.setattr(pod, "generate_batch", observe)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(pod.run_job(config, tmp_path, "fixture-job-token"))
    assert calls.count("/jobs/3/identity") == 1


@pytest.mark.parametrize("mismatch", [None, "source", "main", "fetch"])
def test_worker_fetches_pinned_source_and_rejects_a_different_commit(config, tmp_path, monkeypatch, mismatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    def execute_git(argv, **kwargs):
        assert argv[0] == "git"
        return subprocess.run(argv, check=True, capture_output=True, timeout=kwargs.get("timeout", 60)).stdout

    monkeypatch.setattr(pod, "execute", execute_git)
    repository = tmp_path / "remote-source"
    repository.mkdir()
    pod.git(repository, "init", "-b", "main")
    for version in ("pinned", "latest"):
        filename = "README.md" if mismatch == "main" and version == "pinned" else "main.py"
        (repository / filename).write_text(f"async def generate(*args): return '{version}'\n")
        pod.git(repository, "add", ".")
        pod.git(repository, "-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", version)
        if version == "pinned":
            config = replace(config, source_commit=pod.git(repository, "rev-parse", "HEAD"))
    latest = pod.git(repository, "rev-parse", "HEAD")
    fetch_failed = False

    def external(argv, **kwargs):
        nonlocal fetch_failed
        if "fetch" in argv:
            if mismatch == "fetch" and not fetch_failed:
                fetch_failed = True
                raise RuntimeError("Fixture fetch interrupted")
            argv = [str(repository) if value == config.source_repository else value for value in argv]
            if mismatch == "source":
                argv[-1] = latest
        return execute_git(argv, **kwargs)

    monkeypatch.setattr(pod, "execute", external)
    if mismatch == "fetch":
        with pytest.raises(RuntimeError, match="fetch interrupted"):
            pod.fetch_source(config, tmp_path)
    if mismatch in ("source", "main"):
        with pytest.raises(RuntimeError, match="lacks main.py" if mismatch == "main" else "differs"):
            pod.fetch_source(config, tmp_path)
    else:
        pod.fetch_source(config, tmp_path)
        assert pod.git(tmp_path / "source", "rev-parse", "HEAD") == config.source_commit
        assert (tmp_path / "source/main.py").read_text() == "async def generate(*args): return 'pinned'\n"


@pytest.mark.parametrize("changes", [{"vcpus": 3}, {"go_cpus": 9}, {"prover_contexts": 5},
                                     {"parallel_calls": 33}, {"workers": 0},
                                     {"seed": -1}, {"source_commit": "main"}])
def test_invalid_settings_fail_before_provisioning(config, changes):
    with pytest.raises(ValueError):
        replace(config, **changes)


def test_hardware_limits_and_proof_controls(config, monkeypatch):
    monkeypatch.setattr(pod.sys, "platform", "linux")
    monkeypatch.setattr(pod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(pod.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)
    monkeypatch.setattr(pod.os, "sysconf", lambda name: 4096 if name == "SC_PAGE_SIZE" else 4 * 1024**2)
    assert pod.check_hardware(config)["effective_cpus"] == 8
    monkeypatch.setattr(pod.os, "sched_getaffinity", lambda _: set(range(4)))
    with pytest.raises(RuntimeError, match="CPU/RAM"):
        pod.check_hardware(config)
    assert pod.runtime_environment(config) == {"WITNESS_PROVER_CONTEXTS": "4", "GOMAXPROCS": "8",
                                                    "GOMEMLIMIT": "2048MiB", "PYTHONUNBUFFERED": "1"}
