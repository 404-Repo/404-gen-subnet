import asyncio
import base64
import json
import sys
from dataclasses import replace
from pathlib import PurePosixPath
from types import SimpleNamespace

import bittensor_wallet
import httpx
import pytest
import witness_client
from conftest import access
from miner_runtime import batch, pod
from witness_client import WitnessError


@pytest.fixture
def generation(scenario, tmp_path, monkeypatch):
    directory = tmp_path / "runner"
    directory.mkdir()
    config = pod.Config(
        generation_image=scenario.snapshot.generation_image,
        competition_repository=scenario.inputs.repository,
        competition_branch=scenario.inputs.branch,
        round_number=scenario.inputs.number,
        hotkey=scenario.snapshot.hotkey,
        account_id=scenario.snapshot.account_id,
        witness_url=scenario.snapshot.witness_url,
        workers=scenario.snapshot.workers,
        parallel_calls=scenario.snapshot.parallel_calls,
        parallel_preparations=scenario.snapshot.parallel_preparations,
        prover_contexts=scenario.snapshot.prover_contexts,
        go_cpus=scenario.snapshot.go_cpus,
    )
    (directory / "source").mkdir()
    (directory / "source/main.py").write_text(
        "async def generate(image, seed, witness):\n    return image.decode() + ':' + str(seed)\n"
    )
    state = {
        "rows": {},
        "submitted": [],
        "reads": [],
        "failure": False,
        "git_files": [],
        "source_fetches": [],
        "clients": [],
        "replacements": 0,
        "entered": 0,
        "closed": 0,
        "fail_prompt": None,
        "replacement_times": [],
        "replace_failures": 0,
        "reject_prompt": None,
    }

    class Witness:
        def __init__(self, config, **kwargs):
            assert config["codex_credentials_file"].endswith("private/subscription.json")
            state["clients"].append(kwargs)

        async def __aenter__(self):
            state["entered"] += 1
            return self

        async def __aexit__(self, *args):
            state["closed"] += 1

        async def replace_pod(self):
            state["replacements"] += 1
            if "clock" in state:
                state["replacement_times"].append(state["clock"][0])
            if state["replace_failures"]:
                state["replace_failures"] -= 1
                raise RuntimeError("Fixture witness replacement unavailable")

        async def get_submitted(self):
            return state["rows"]

        async def submit(self, image, result):
            name = next(name for name, raw in scenario.images.items() if raw == image)
            if state["failure"]:
                raise WitnessError("Fixture save service unavailable", code="witness_unavailable", status_code=503)
            if state["fail_prompt"] == name:
                raise httpx.ReadTimeout("Fixture save request timed out")
            state["submitted"].append(result)
            if state["reject_prompt"] == name:
                state["rows"][name] = {"failure_reason": "render_rejected", "js": name + ".js", "size": len(result)}
                raise WitnessError("Fixture final validation rejection", code="validation_failed", status_code=422)
            state["rows"][name] = (
                {"failure_reason": "generation_failed"}
                if result is None
                else {"js": f"{name}.js", "views": name, "size": len(result)}
            )
            return {"saved": True}

    async def download(transport, request):
        git = scenario.deps.git
        if request.url.host == "api.github.com":
            repo = f"/repos/{config.competition_repository}"
            if request.url.path == f"{repo}/commits/{config.competition_branch}":
                return httpx.Response(200, json={"sha": git.ref_sha})
            assert request.url.path.startswith(f"{repo}/contents/")
            assert request.url.params["ref"] == git.ref_sha
            path = request.url.path.removeprefix(f"{repo}/contents/")
            state["git_files"].append(path)
            if path not in git.files:
                return httpx.Response(404)
            return httpx.Response(200, text=git.files[path])
        assert request.url.host in ("images.test", "raw.githubusercontent.com")
        name = PurePosixPath(request.url.path).stem
        state["reads"].append(name)
        return httpx.Response(200, content=scenario.images[name])

    def fetch_source(repository, commit, target):
        assert target == directory
        state["source_fetches"].append((repository, commit))

    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=scenario.snapshot.hotkey))
    monkeypatch.setattr(bittensor_wallet, "Wallet", lambda **kwargs: wallet)
    monkeypatch.setattr(witness_client, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(pod, "fetch_source", fetch_source)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)

    async def once(config, directory):
        witness = witness_client.AsyncWitnessClient(
            {"codex_credentials_file": str(directory / "private/subscription.json")}
        )
        await batch.run_pass(
            pod.load_miner(directory / "source").generate,
            dict(scenario.prompts),
            42,
            client=witness,
            workers=config.workers,
            max_prompt_bytes=batch.MAX_PROMPT_DOWNLOAD_BYTES,
        )

    async def run(config):
        await pod.run_job(
            config,
            directory,
            hotkey=base64.b64encode(b"fixture hotkey").decode(),
            access_token=access(4_000_000_000).access_token,
        )

    state["once"] = once
    state["run"] = run
    return config, directory, state


async def test_terminal_validation_rejection_and_none_finish_without_retrying(generation, monkeypatch):
    config, directory, state = generation
    state["reject_prompt"] = "chair"
    (directory / "source/main.py").write_text(
        "async def generate(image, seed, witness):\n" "    return None if image == b'lamp' else image.decode()\n"
    )

    async def retry(delay):
        raise AssertionError("Terminal outcomes must finish without another retry")

    class Asyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        sleep = staticmethod(retry)

    monkeypatch.setattr(pod, "asyncio", Asyncio())
    await state["run"](config)
    assert state["rows"]["chair"]["failure_reason"] == "render_rejected"
    assert state["rows"]["lamp"]["failure_reason"] == "generation_failed"
    assert sorted(state["reads"]) == ["chair", "lamp"]
    assert state["replacements"] == 1
    assert state["entered"] == state["closed"] == 1


@pytest.mark.parametrize("replacement_failures", [0, 3])
@pytest.mark.parametrize("pass_duration", [0, 1500])
@pytest.mark.parametrize("terminal_rejection", [False, True])
async def test_worker_retries_pending_prompts_with_a_single_client_and_twenty_minute_witness_spacing(
    generation, monkeypatch, replacement_failures, pass_duration, terminal_rejection
):
    config, directory, state = generation
    state["fail_prompt"] = "chair"
    state["replace_failures"] = replacement_failures
    state["clock"] = [0.0]
    state["reject_prompt"] = "lamp" if terminal_rejection else None
    imports = directory / "imports.txt"
    (directory / "source/main.py").write_text(
        "from pathlib import Path\n"
        f"with Path({str(imports)!r}).open('a') as stream:\n"
        "    stream.write('loaded\\n')\n"
        "async def generate(image, seed, witness):\n"
        + (
            "    return image.decode()\n"
            if terminal_rejection
            else "    return None if image == b'lamp' else image.decode()\n"
        )
    )
    download = httpx.AsyncHTTPTransport.handle_async_request
    first_download = True

    async def request(transport, request):
        nonlocal first_download
        response = await download(transport, request)
        if first_download and request.url.host != "api.github.com":
            state["clock"][0] += pass_duration
            first_download = False
        return response

    waits = []

    async def wait_for_retry(delay):
        if delay <= 60:
            await asyncio.Event().wait()
        else:
            waits.append(delay)
            state["clock"][0] += delay

    actual_replace = state["replace_failures"]

    class Asyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        sleep = staticmethod(wait_for_retry)

    monkeypatch.setattr(pod, "asyncio", Asyncio())
    monkeypatch.setattr(pod, "time", SimpleNamespace(monotonic=lambda: state["clock"][0]), raising=False)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", request)
    # A long first pass still needs its failed prompt to become available on replacement.
    witness_class = witness_client.AsyncWitnessClient
    replace_pod = witness_class.replace_pod

    async def replace_witness(self):
        await replace_pod(self)
        if state["replacements"] > actual_replace + 1:
            state["fail_prompt"] = None

    monkeypatch.setattr(witness_class, "replace_pod", replace_witness)
    await state["run"](config)
    assert imports.read_text().splitlines() == ["loaded"]
    assert set(state["rows"]) == {"chair", "lamp"}
    assert state["rows"]["lamp"]["failure_reason"] == ("render_rejected" if terminal_rejection else "generation_failed")
    assert state["reads"].count("lamp") == 1
    assert state["reads"].count("chair") == 2
    assert len(state["clients"]) == 1
    assert state["entered"] == state["closed"] == 1
    assert state["replacement_times"][0] == 0
    assert all(
        right - left >= 1200
        for left, right in zip(state["replacement_times"], state["replacement_times"][1:], strict=False)
    )
    assert len(state["replacement_times"]) == 2 + actual_replace
    assert (not waits) is (pass_duration >= 1200 and not replacement_failures)


async def test_one_configured_client_and_fresh_witness_pod_per_pass(generation):
    config, _, state = generation
    await state["run"](config)
    assert len(state["clients"]) == 1
    assert state["entered"] == state["closed"] == 1
    assert state["replacements"] == 1
    assert state["clients"][0]["parallel_calls"] == config.parallel_calls
    assert state["clients"][0]["parallel_preparations"] == config.parallel_preparations
    assert len(state["submitted"]) == 2


async def test_prompt_exception_is_skipped_for_the_pass_and_retried_on_the_next_pod(generation):
    config, directory, state = generation
    (directory / "source/main.py").write_text(
        "import asyncio\n"
        "active = peak = 0\n"
        "calls = []\n"
        "async def generate(image, seed, witness):\n"
        "    global active, peak\n"
        "    calls.append(image)\n"
        "    active += 1\n"
        "    peak = max(peak, active)\n"
        "    await asyncio.sleep(0)\n"
        "    active -= 1\n"
        "    if image == b'chair':\n"
        "        raise RuntimeError('Fixture miner failure')\n"
        "    return image.decode()\n"
    )
    with pytest.raises(RuntimeError, match="prompts remain"):
        await state["once"](config, directory)
    assert sorted(sys.modules["main"].calls) == [b"chair", b"lamp"]
    assert sys.modules["main"].peak == config.workers
    assert set(state["rows"]) == {"lamp"}
    (directory / "source/main.py").write_text("async def generate(image, seed, witness):\n    return image.decode()\n")
    state["reads"].clear()
    await state["once"](config, directory)
    assert state["reads"] == ["chair"]
    assert set(state["rows"]) == {"chair", "lamp"}


async def test_worker_uses_git_leader_and_seed_and_skips_saved_ids(generation, scenario):
    config, _, state = generation
    files = scenario.deps.git.files
    leader = scenario.leader.model_copy(update={"repo": "test/new-leader", "commit": "c" * 40, "effective_block": 2})
    files["leader.json"] = json.dumps([scenario.leader.model_dump(), leader.model_dump()])
    files["rounds/1/seed.json"] = '{"seed":97}'
    state["rows"]["lamp"] = {"failure_reason": "generation_failed"}
    await state["run"](config)
    assert state["source_fetches"] == [(leader.repo, leader.commit)]
    assert state["reads"] == ["chair"]
    assert state["submitted"] == ["chair:97"]


async def test_workers_download_and_submit_without_prefetching_all_images(generation, scenario, monkeypatch):
    config, directory, state = generation
    events = []

    async def download(transport, request):
        prompt = PurePosixPath(request.url.path).stem
        events.append(prompt)
        if prompt == "lamp":
            assert "chair" in state["rows"]
        return httpx.Response(200, content=scenario.images[prompt])

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)
    await state["once"](replace(config, workers=1), directory)
    assert events == ["chair", "lamp"]
    assert set(state["rows"]) == {"chair", "lamp"}


@pytest.mark.parametrize(
    "chair_url",
    [
        "images/chair.png",
        "https://raw.githubusercontent.com/test/competition/main/images/chair.png",
        "https://github.com/Test/Competition/blob/main/images/chair.png",
    ],
)
async def test_worker_pins_competition_prompt_urls_to_the_git_revision(generation, scenario, monkeypatch, chair_url):
    config, _, state = generation
    scenario.deps.git.files["rounds/1/prompts.txt"] = f"{chair_url}\nhttps://images.test/lamp.png"
    downloads = []
    original_download = httpx.AsyncHTTPTransport.handle_async_request

    async def download(transport, request):
        if request.url.host != "api.github.com":
            downloads.append(str(request.url))
        return await original_download(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)
    await state["run"](replace(config, workers=1))
    assert downloads == [
        f"https://raw.githubusercontent.com/test/competition/{scenario.deps.git.ref_sha}/images/chair.png",
        "https://images.test/lamp.png",
    ]
    assert set(state["rows"]) == {"chair", "lamp"}


async def test_worker_accepts_an_uppercase_https_scheme(generation, scenario):
    config, _, state = generation
    files = scenario.deps.git.files
    files["rounds/1/prompts.txt"] = files["rounds/1/prompts.txt"].replace("https:", "HTTPS:", 1)

    await state["run"](config)

    assert set(state["rows"]) == {"chair", "lamp"}


async def test_worker_stops_at_the_prompt_download_limit(generation, monkeypatch):
    config, directory, state = generation
    original_download = httpx.AsyncHTTPTransport.handle_async_request
    chunk_bytes = 8 * 1024 * 1024
    reads = []

    class OversizedImage(httpx.AsyncByteStream):
        async def __aiter__(self):
            for size in (chunk_bytes, chunk_bytes, 1, 1):
                reads.append(size)
                yield bytes(size)

    async def download(transport, request):
        if request.url.path == "/chair.png":
            return httpx.Response(200, stream=OversizedImage())
        return await original_download(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)
    with pytest.raises(RuntimeError, match="1 prompts remain"):
        await state["once"](config, directory)

    assert set(state["rows"]) == {"lamp"}
    assert reads == [chunk_bytes, chunk_bytes, 1]


async def test_save_outage_does_not_manufacture_failure_results(generation):
    config, directory, state = generation
    state["failure"] = True
    with pytest.raises(RuntimeError, match="prompts remain"):
        await state["once"](config, directory)
    assert state["rows"] == {}
    state["failure"] = False
    await state["once"](config, directory)
    assert set(state["rows"]) == {"chair", "lamp"}


async def test_none_is_saved_as_terminal_and_skipped_on_resume(generation):
    config, directory, state = generation
    (directory / "source/main.py").write_text("async def generate(image, seed, witness):\n    return None\n")
    await state["once"](config, directory)
    assert state["submitted"] == [None, None]
    await state["once"](config, directory)
    assert state["submitted"] == [None, None]


@pytest.mark.parametrize("number,stage", [(2, "miner_generation"), (1, "collecting")])
async def test_worker_exits_when_the_requested_round_is_no_longer_generating(generation, scenario, number, stage):
    config, _, state = generation
    scenario.deps.git.files["state.json"] = json.dumps({"current_round": number, "stage": stage})

    await state["run"](config)

    assert state["git_files"] == ["state.json"]
    assert state["source_fetches"] == []
    assert state["clients"] == []
    assert state["submitted"] == []
