import base64
import json
import secrets
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from conftest import access
from miner_runtime.pod import Config

from leader_generation_service import server
from leader_generation_service.models import Allocation, Job
from leader_generation_service.store import atomic_write


@pytest.fixture
def endpoint(scenario, settings, monkeypatch):
    job = Job(snapshot=scenario.snapshot, allocations=[Allocation(phase="running", token=secrets.token_urlsafe(32))])
    scenario.deps.store.save(job)
    directory = scenario.deps.store.run_directory(job)
    config = Config(
        generation_image=job.snapshot.generation_image,
        source_repository="https://github.com/test/incumbent",
        source_commit=job.snapshot.leader.commit,
        prompts={prompt.id: prompt.sha256 for prompt in scenario.inputs.prompts},
        seed=scenario.inputs.seed,
        hotkey=job.snapshot.hotkey,
        account_id=job.snapshot.account_id,
        credentials_url="https://leader.test/v1/jobs/1",
        witness_url=settings.witness_url,
        round_inputs_fingerprint=scenario.inputs.fingerprint,
    )
    atomic_write(directory / "config.json", json.dumps(asdict(config)).encode())
    hotkey = settings.wallet_path / settings.wallet_name / "hotkeys" / settings.wallet_hotkey
    hotkey.parent.mkdir(parents=True)
    hotkey.write_bytes(b"fixture-private-hotkey")
    wallet = SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address=scenario.snapshot.hotkey),
        hotkey_file=SimpleNamespace(is_encrypted=lambda: False),
    )
    monkeypatch.setattr(server, "Wallet", lambda **kwargs: wallet)
    app = server.application(scenario.controller, settings)

    async def request(resource="credentials", *, token=job.allocation.token, number=1):
        path = f"/v1/jobs/{number}/{resource}"
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        initial = make_mocked_request("GET", path, headers=headers, app=app)
        match = await app.router.resolve(initial)
        request = make_mocked_request("GET", path, headers=headers, app=app, match_info=match)
        try:
            return await match.handler(request)
        except web.HTTPException as error:
            return error

    return job, request, wallet


async def test_job_credentials_are_access_only_and_follow_service_renewal(endpoint, scenario):
    _, request, _ = endpoint
    first = await request()
    assert first.status == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert set(json.loads(first.body)) == {"access_token", "account_id"}
    renewed = access(time.time() + 7200)
    scenario.deps.credentials._current = None
    scenario.deps.credentials.load = lambda: renewed
    second = await request()
    assert second.status == 200
    assert json.loads(second.body)["access_token"] == renewed.access_token
    assert second.body != first.body


async def test_identity_is_scoped_to_the_frozen_operator(endpoint):
    _, request, wallet = endpoint
    response = await request("identity")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.body) == {"hotkey": base64.b64encode(b"fixture-private-hotkey").decode()}
    wallet.hotkey.ss58_address = "different-operator"
    response = await request("identity")
    assert response.status == 503
    assert b"fixture-private-hotkey" not in response.body


async def test_public_job_configuration_keeps_runtime_and_inputs_pinned(endpoint, scenario):
    _, request, _ = endpoint
    response = await request("job")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    config = json.loads(response.body)
    assert config["generation_image"] == scenario.snapshot.generation_image
    assert config["round_inputs_fingerprint"] == scenario.inputs.fingerprint
    assert config["source_commit"] == scenario.snapshot.leader.commit


@pytest.mark.parametrize(
    "problem", ["missing", "wrong", "other_job", "replaced", "terminated", "published", "stopped", "legacy"]
)
async def test_job_token_cannot_read_another_or_inactive_attempt(endpoint, scenario, problem):
    job, request, _ = endpoint
    token, number = job.allocation.token, 1
    if problem == "missing":
        token = None
    elif problem == "wrong":
        token = secrets.token_urlsafe(32)
    elif problem == "other_job":
        number = 2
    elif problem == "replaced":
        job.allocations.append(Allocation(phase="running", token=secrets.token_urlsafe(32)))
    elif problem == "terminated":
        job.allocation.phase = "terminated"
    elif problem == "published":
        job.published = True
    elif problem == "stopped":
        job.stopped = True
    else:
        job.allocation.token = None
    scenario.deps.store.save(job)
    for resource in ("identity", "credentials", "job"):
        response = await request(resource, token=token, number=number)
        assert response.status in (401, 404)
        assert response.headers["Cache-Control"] == "no-store"
        assert b"access_token" not in response.body
        assert b"hotkey" not in response.body


async def test_changed_provider_account_is_not_delivered(endpoint, scenario):
    _, request, _ = endpoint
    scenario.deps.credentials.load = lambda: access(time.time() + 3600, "def456")
    response = await request()
    assert response.status == 503
    assert b"access_token" not in response.body


async def test_attempt_revoked_during_credential_refresh_gets_no_secret(endpoint, scenario):
    job, request, _ = endpoint
    original = scenario.deps.credentials.load

    def revoke():
        job.allocation.phase = "terminated"
        scenario.deps.store.save(job)
        return original()

    scenario.deps.credentials.load = revoke
    response = await request()
    assert response.status == 404
    assert b"access_token" not in response.body
