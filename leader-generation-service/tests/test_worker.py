import asyncio
import base64
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
        source_repository="https://github.com/test/incumbent",
        source_commit=scenario.snapshot.leader.commit,
        prompts={prompt.id: prompt.sha256 for prompt in scenario.inputs.prompts},
        seed=scenario.inputs.seed,
        hotkey=scenario.snapshot.hotkey,
        account_id=scenario.snapshot.account_id,
        witness_url=scenario.snapshot.witness_url,
        workers=scenario.snapshot.workers,
        parallel_calls=scenario.snapshot.parallel_calls,
        parallel_preparations=scenario.snapshot.parallel_preparations,
        prover_contexts=scenario.snapshot.prover_contexts,
        go_cpus=scenario.snapshot.go_cpus,
        round_inputs_fingerprint=scenario.inputs.fingerprint,
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
        "bad_image": None,
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

        def renderer_transport(self):
            context = {
                "saving_enabled": True,
                "round_inputs": scenario.inputs.model_dump(mode="json"),
                "submission_commit": None,
                "prompts": [
                    {**prompt.model_dump(), "url": f"https://saved-images.test/{prompt.id}"}
                    for prompt in scenario.inputs.prompts
                ],
            }
            return httpx.MockTransport(lambda request: httpx.Response(200, json=context))

    async def download(transport, request):
        name = PurePosixPath(request.url.path).stem
        state["reads"].append(name)
        return httpx.Response(200, content=b"changed" if name == state["bad_image"] else scenario.images[name])

    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=scenario.snapshot.hotkey))
    monkeypatch.setattr(bittensor_wallet, "Wallet", lambda **kwargs: wallet)
    monkeypatch.setattr(batch, "Wallet", lambda **kwargs: wallet)
    monkeypatch.setattr(witness_client, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(batch, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)

    async def once(config, directory):
        witness = witness_client.AsyncWitnessClient(
            {"codex_credentials_file": str(directory / "private/subscription.json")}
        )
        await pod.generate_pass(config, witness, pod.load_miner(directory / "source").generate)

    state["once"] = once
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
    await pod.generate_batch(config, directory)
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
        if first_download:
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
    await pod.run_job(
        config,
        directory,
        hotkey=base64.b64encode(b"fixture hotkey").decode(),
        access_token=access(4_000_000_000).access_token,
    )
    assert imports.read_text().splitlines() == ["loaded"]
    assert set(state["rows"]) == set(config.prompts)
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
    config, directory, state = generation
    await pod.generate_batch(config, directory)
    assert len(state["clients"]) == 1
    assert state["entered"] == state["closed"] == 1
    assert state["replacements"] == 1
    assert state["clients"][0]["parallel_calls"] == config.parallel_calls
    assert state["clients"][0]["parallel_preparations"] == config.parallel_preparations
    assert len(state["submitted"]) == len(config.prompts)


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
    assert state["replacements"] == 2
    assert set(state["rows"]) == set(config.prompts)


async def test_real_batch_uses_source_seed_and_skips_witness_saved_ids(generation):
    config, directory, state = generation
    state["rows"]["lamp"] = {"failure_reason": "generation_failed"}
    await state["once"](config, directory)
    assert state["reads"] == ["chair"]
    assert state["submitted"] == ["chair:42"]
    state["reads"].clear()
    await state["once"](config, directory)
    assert state["reads"] == []
    assert state["submitted"] == ["chair:42"]


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
    assert set(state["rows"]) == set(config.prompts)


async def test_worker_downloads_the_frozen_competition_urls(generation, scenario, monkeypatch):
    config, directory, state = generation
    downloads = []
    original_download = httpx.AsyncHTTPTransport.handle_async_request

    async def download(transport, request):
        downloads.append(str(request.url))
        return await original_download(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)
    await state["once"](replace(config, workers=1), directory)
    assert downloads == [prompt.source_url for prompt in scenario.inputs.prompts]
    assert set(state["rows"]) == set(config.prompts)


async def test_worker_accepts_an_uppercase_https_scheme(generation, scenario):
    config, directory, state = generation
    chair, lamp = scenario.inputs.prompts
    chair = chair.model_copy(update={"source_url": chair.source_url.replace("https:", "HTTPS:", 1)})
    scenario.inputs = scenario.inputs.model_copy(update={"prompts": (chair, lamp)})

    await state["once"](config, directory)

    assert set(state["rows"]) == set(config.prompts)


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


async def test_hash_mismatch_remains_pending_while_other_prompts_save(generation):
    config, directory, state = generation
    state["bad_image"] = "chair"
    with pytest.raises(RuntimeError, match="prompts remain"):
        await state["once"](config, directory)
    assert set(state["rows"]) == {"lamp"}
    state["bad_image"] = None
    state["reads"].clear()
    await state["once"](config, directory)
    assert state["reads"] == ["chair"]
    assert len(state["submitted"]) == 2


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


@pytest.mark.parametrize("rollover", ["download", "retry", "confirmation"])
async def test_every_witness_connection_checks_frozen_round_before_saved_ids(
    generation, scenario, monkeypatch, rollover
):
    config, directory, state = generation
    generated = directory / "generated.txt"
    (directory / "source/main.py").write_text(
        "from pathlib import Path\n"
        "async def generate(image, seed, witness):\n"
        f"    with Path({str(generated)!r}).open('a') as stream:\n"
        "        stream.write(str(seed) + '\\n')\n"
        "    return image.decode() + ':' + str(seed)\n"
    )
    rounds = {
        1: scenario.inputs,
        2: scenario.inputs.model_copy(update={"number": 2, "round": "test-2", "seed": 99}),
    }
    current = [1]
    rows = {1: {}, 2: {name: {"failure_reason": "generation_failed"} for name in scenario.images}}
    lookups, contexts, submissions = [], [], []

    class Witness:
        def __init__(self, config, **kwargs):
            self.inputs = rounds[current[0]]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def replace_pod(self):
            pass

        def renderer_transport(self):
            self.inputs = rounds[current[0]]

            def context(request):
                contexts.append(self.inputs.number)
                return httpx.Response(
                    200,
                    json={
                        "saving_enabled": True,
                        "round_inputs": self.inputs.model_dump(mode="json"),
                        "submission_commit": None,
                        "prompts": [
                            {**prompt.model_dump(), "url": f"https://saved-images.test/{prompt.id}"}
                            for prompt in self.inputs.prompts
                        ],
                    },
                )

            return httpx.MockTransport(context)

        async def get_submitted(self):
            lookups.append(self.inputs.number)
            return rows[self.inputs.number]

        async def submit(self, image, result):
            submissions.append(self.inputs.number)
            if rollover == "retry":
                current[0] = 2
                raise RuntimeError("Fixture save connection lost during rollover")
            name = next(name for name, raw in scenario.images.items() if raw == image)
            rows[self.inputs.number][name] = {"failure_reason": "generation_failed"}
            if rollover == "confirmation" and len(rows[1]) == len(scenario.images):
                current[0] = 2
            return {"saved": True}

    async def download(transport, request):
        if rollover == "download":
            current[0] = 2
        return httpx.Response(200, content=scenario.images[PurePosixPath(request.url.path).stem])

    monkeypatch.setattr(witness_client, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(batch, "AsyncWitnessClient", Witness)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", download)
    with pytest.raises(ValueError, match="Witness seed differs"):
        await state["once"](config, directory)
    assert 2 in contexts
    assert lookups == [1]
    assert submissions == {"download": [], "retry": [1], "confirmation": [1, 1]}[rollover]
    assert (generated.read_text().splitlines() if generated.exists() else []) == (
        {"download": [], "retry": ["42"], "confirmation": ["42", "42"]}[rollover]
    )
