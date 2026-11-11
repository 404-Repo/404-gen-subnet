import base64
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
from subnet_common.witness.models import Pod

from leader_generation_service.credentials import AccessCredentials, CredentialManager
from leader_generation_service.generation_iteration import Dependencies
from leader_generation_service.round_runner import RoundRunner
from leader_generation_service.settings import Settings
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
        RUNPOD_PROVIDER_API_KEY="fixture-runpod",
        cdn_url="https://cdn.test",
        witness_url="https://witness.test",
        state_directory=tmp_path / "state",
        LEADER_CODEX_HOME=tmp_path / "login",
        wallet_path=tmp_path / "wallets",
    )


class Runner:
    def __init__(self, image):
        self.image = image
        self.startup_cleanups = 0
        self.started = []
        self.terminated = []

    async def cleanup(self):
        self.startup_cleanups += 1

    async def start(self, number, hotkey, credentials, *, min_validity_seconds):
        self.started.append(number)
        return Pod(f"runner-{number}", f"leader-runner-fixture-{number}", self.image, "RUNNING", {})

    async def terminate(self, pod):
        self.terminated.append(pod)


@pytest.fixture
def scenario(settings, tmp_path):
    hotkey = Keypair.create_from_uri("//Alice").ss58_address
    images = {"chair": b"chair", "lamp": b"lamp"}
    leader = LeaderEntry(hotkey=hotkey, repo="test/incumbent", commit="b" * 40, weight=1, effective_block=1)
    prompts = {name: f"https://images.test/{name}.png" for name in images}
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
        "rounds/1/prompts.txt": "\n".join(prompts.values()),
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
    runner = Runner(settings.generation_image)
    state = {"saved": {}, "block": 10, "index_reads": 0}

    async def head():
        return state["block"], time.time()

    def request(request):
        assert request.url == f"https://cdn.test/sandbox/rounds/test-1/{hotkey}/submitted.json"
        state["index_reads"] += 1
        return httpx.Response(200, json=state["saved"])

    credentials = CredentialManager(tmp_path, "fixture-codex", load=lambda _: access(time.time() + 86400))
    http = httpx.AsyncClient(transport=httpx.MockTransport(request))
    dependencies = Dependencies(git, runner, credentials, SavedResults(http, settings.cdn_url), head, hotkey)
    controller = RoundRunner(dependencies, settings, GracefulShutdown())
    return SimpleNamespace(
        deps=dependencies,
        controller=controller,
        state=state,
        images=images,
        prompts=prompts,
        leader=leader,
        http=http,
    )
