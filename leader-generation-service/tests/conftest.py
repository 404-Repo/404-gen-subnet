import base64
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from bittensor_wallet import Keypair
from pydantic import SecretStr
from subnet_common.competition.leader import LeaderEntry
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.discord import DiscordWebhook
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient
from subnet_common.witness.rounds import PromptImage, RoundInputs

from leader_generation_service.credentials import AccessCredentials, CredentialManager
from leader_generation_service.generation_iteration import Dependencies
from leader_generation_service.models import Allocation, Snapshot
from leader_generation_service.round_runner import RoundRunner
from leader_generation_service.settings import Settings
from leader_generation_service.store import JobStore
from leader_generation_service.witness import SavedResults


def access(expiry, account="abc123"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return AccessCredentials("fixture." + payload + "." + "s" * 64, account)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected external HTTP, compute allocation or real credential refresh")

    async def async_forbidden(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_forbidden)
    monkeypatch.setattr("witness_client.auth._refresh_codex", forbidden)
    monkeypatch.setattr("leader_generation_service.credentials._refresh_codex", forbidden)


@pytest.fixture
def discord(scenario, monkeypatch):
    state = SimpleNamespace(messages=[], status=200)

    async def deliver(transport, request):
        assert request.url == "https://discord.test/progress"
        state.messages.append(json.loads(request.content))
        return httpx.Response(state.status, json={})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deliver)
    scenario.deps.discord = DiscordWebhook("https://discord.test/progress")
    return state


@pytest.fixture
def clock(monkeypatch):
    value = [time.time()]
    monkeypatch.setattr(time, "time", lambda: value[0])
    return value


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        github_token=SecretStr("fixture-github"),
        github_repo="test/competition",
        generation_image="registry.test/leader@sha256:" + "d" * 64,
        runpod_api_key="fixture-runpod",
        cdn_url="https://cdn.test",
        witness_url="https://witness.test",
        state_directory=tmp_path / "state",
        LEADER_CODEX_HOME=tmp_path / "login",
        wallet_path=tmp_path / "wallets",
    )


class Runner:
    def __init__(self, store):
        self.store = store
        self.ensured = []
        self.started = []
        self.terminated = []
        self.on_ensure = None

    async def ensure(self, job):
        self.ensured.append(job)
        if job.allocation is None or job.allocation.phase == "terminated":
            job.allocations.append(Allocation(pod_id="runner", phase="active"))
        self.store.save(job)
        if self.on_ensure:
            self.on_ensure()

    async def start(self, job, credentials, *, min_validity_seconds):
        self.started.append(job)
        job.allocation.phase = "active"
        self.store.save(job)

    async def terminate(self, job):
        self.terminated.append(job.snapshot.inputs.number)
        if job.allocation:
            job.allocation.phase = "terminated"
            self.store.save(job)


@pytest.fixture
def scenario(settings, tmp_path):
    hotkey = Keypair.create_from_uri("//Alice").ss58_address
    images = {"chair": b"chair", "lamp": b"lamp"}
    inputs = RoundInputs(
        repository="test/competition",
        branch="main",
        revision="a" * 40,
        round="test-1",
        number=1,
        seed=42,
        prompts=tuple(
            PromptImage(
                id=name,
                source_url=f"https://images.test/{name}.png",
                sha256=hashlib.sha256(raw).hexdigest(),
                image_key=f"sandbox/competition-inputs/v1/{hashlib.sha256(raw).hexdigest()}",
            )
            for name, raw in images.items()
        ),
        generation_deadline_block=100,
        storage_prefix="sandbox/",
    )
    leader = LeaderEntry(hotkey=hotkey, repo="test/incumbent", commit="b" * 40, weight=1, effective_block=1)
    files = {
        "state.json": CompetitionState(current_round=1, stage=RoundStage.MINER_GENERATION).model_dump_json(),
        "config.json": json.dumps(
            {
                "name": "test",
                "description": "fixture",
                "first_evaluation_date": "2026-01-01",
                "last_competition_date": "2027-01-01",
                "generation_stage_minutes": 180,
                "win_margin": 0.1,
                "prompts_per_round": 2,
                "carryover_prompts": 0,
                "leader_generation_hotkey": hotkey,
            }
        ),
        "leader.json": json.dumps([leader.model_dump()]),
        "rounds/1/prompts.txt": "\n".join(p.source_url for p in inputs.prompts),
        "rounds/1/seed.json": '{"seed":42}',
        "rounds/1/schedule.json": json.dumps(
            {
                "earliest_reveal_block": 1,
                "latest_reveal_block": 2,
                "generation_deadline_block": 100,
                "witness_storage_prefix": "sandbox/",
            }
        ),
    }
    git = MockGitHubClient(files, ref_sha="a" * 40)
    store = JobStore(settings.state_directory)
    runner = Runner(store)
    state = {"saved": {}, "block": 10, "input_reads": 0, "index_reads": 0}

    async def read_inputs():
        state["input_reads"] += 1
        return inputs

    async def head():
        return state["block"], time.time()

    def request(request):
        assert request.url == f"https://cdn.test/sandbox/rounds/test-1/{hotkey}/submitted.json"
        state["index_reads"] += 1
        return httpx.Response(200, json=state["saved"])

    credentials = CredentialManager(tmp_path, "fixture-codex", load=lambda _: access(time.time() + 86400))
    http = httpx.AsyncClient(transport=httpx.MockTransport(request))
    dependencies = Dependencies(
        git, store, runner, credentials, read_inputs, SavedResults(http, settings.cdn_url), head, hotkey
    )
    snapshot = Snapshot(
        state_ref="a" * 40,
        generation_image=settings.generation_image,
        leader=leader,
        inputs=inputs,
        hotkey=hotkey,
        account_id="abc123",
        wallet_name=settings.wallet_name,
        wallet_hotkey=settings.wallet_hotkey,
        witness_url=settings.witness_url,
        workers=2,
        parallel_calls=2,
        parallel_preparations=1,
        prover_contexts=1,
        go_cpus=2,
        go_memory_mib=2048,
    )
    controller = RoundRunner(dependencies, settings, GracefulShutdown())
    return SimpleNamespace(
        deps=dependencies,
        controller=controller,
        state=state,
        snapshot=snapshot,
        inputs=inputs,
        images=images,
        http=http,
    )
