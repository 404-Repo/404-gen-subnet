"""Local subprocess integration with saved model captures and render outcomes."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, Self

import boto3
import httpx
import pytest
from botocore.awsrequest import AWSResponse
from botocore.compat import HTTPHeaders
from botocore.exceptions import ClientError, ReadTimeoutError
from replay_fixtures import PRICING, SCOPE, Capture
from subnet_common.github import GitHubClient
from subnet_common.witness.rounds import PromptImage, RoundInputs
from test_manual_replay import Stored, archive, round_fixture
from test_replay_command import png_bytes, png_input, replay_requests
from witness_client.calls import canonical

from validation_worker import manual_replay as local_replay
from validation_worker.manual_replay import Log, ReplayProcesses, execute_prompt
from validation_worker.replay_evidence import SavedRenderer


PNG = b"\x89PNG\r\n\x1a\nfixture"
RID = "witness@sha256:" + "a" * 64
SOURCE = """import os
import sys
from renderer_client import RenderOptions

async def generate(image, seed, witness):
    assert image == b"PNG" and seed == 42
    for key in ("RENDERER_API_KEY", "R2_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "DISCORD_WEBHOOK_URL"):
        assert key not in os.environ
    print("miner stdout", flush=True)
    print("miner stderr", file=sys.stderr, flush=True)
    os.write(1, b"native stdout\\n")
    print("export default function minerGenerated(THREE) {", flush=True)
    print("  return new THREE.Group();", flush=True)
    print("}", flush=True)
    await witness.render("candidate", RenderOptions(lighting="neutral"))
    reply = await witness.responses.create(model="fixture", input="image")
    return reply.output_text
"""


@pytest.mark.parametrize(
    "case",
    [
        "async",
        "sync",
        "png",
        "tolerance",
        "request",
        "output",
        "missing_render",
        "render_rejection",
        "caught_missing_render",
        "caught_missing_render_sync",
        "caught_missing_render_output_mismatch",
        "caught_corrupt_render",
        "missing_final_render",
        "none",
        "timeout",
        "http",
        "provider",
        "capture",
        "uncaptured",
        "validation",
        "bad_request",
        "index_miss",
    ],
)
def test_complete_local_replay_bridge(
    tmp_path: Path,
    case: Literal[
        "async",
        "sync",
        "png",
        "tolerance",
        "request",
        "output",
        "missing_render",
        "render_rejection",
        "caught_missing_render",
        "caught_missing_render_sync",
        "caught_missing_render_output_mismatch",
        "caught_corrupt_render",
        "missing_final_render",
        "none",
        "timeout",
        "http",
        "provider",
        "capture",
        "uncaptured",
        "validation",
        "bad_request",
        "index_miss",
    ],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_http(*args: object, **kwargs: object) -> None:
        raise AssertionError("Saved render replay must not contact a live renderer")

    monkeypatch.setattr(httpx.Client, "send", unexpected_http)
    size = (200, 100) if case == "tolerance" else (4, 4)
    original = png_input(compression=0, size=size)
    image = png_bytes(compression=0) if case in ("png", "tolerance") else b"PNG"
    recorded_failures: dict[str, Literal["http", "provider", "capture", "bad_request"]] = {
        "http": "http",
        "provider": "provider",
        "capture": "capture",
        "bad_request": "bad_request",
    }
    recording = replay_requests(
        tmp_path,
        model_input=original if case in ("png", "tolerance") else "image",
        image=image,
        failure=recorded_failures.get(case),
    )
    if case == "index_miss":
        stale = recording.inventory.model_copy(deep=True)
        stale.captures.clear()
        (tmp_path / "bundle/requests.json").write_text(stale.model_dump_json())
    source = tmp_path / "source"
    source.mkdir()
    code = SOURCE
    expected: str | None = "0"
    if case in ("png", "tolerance"):
        code = SOURCE.replace('assert image == b"PNG" and seed == 42', "assert seed == 42")
        code = code.replace(
            'input="image"',
            "input=" + repr(png_input("pixel" if case == "tolerance" else "compression", compression=9, size=size)),
        )
    if case == "sync":
        code = SOURCE.replace(
            "async def generate(image, seed, witness):", "def generate(image, seed, witness, renderer):"
        )
        code = code.replace("await witness.render", "renderer.render").replace(
            "await witness.responses.create", "witness.responses.create"
        )
    elif case == "request":
        code = SOURCE.replace('input="image"', 'input="changed"')
    elif case == "output":
        expected = "different independently saved JavaScript"
    elif case == "none":
        code, expected = "async def generate(image, seed, witness): return None", None
    elif case == "timeout":
        code = "import time\ndef generate(image, seed, witness, renderer): time.sleep(30)"
    elif case == "caught_corrupt_render":
        code = """from renderer_client import RenderOptions
async def generate(image, seed, witness):
    try:
        await witness.render("candidate", RenderOptions(lighting="neutral"))
    except Exception:
        pass
    return "0"
"""
    elif case in ("caught_missing_render", "caught_missing_render_sync", "caught_missing_render_output_mismatch"):
        code = """from renderer_client import RenderOptions, RenderUnavailable
async def generate(image, seed, witness):
    try:
        await witness.render("missing", RenderOptions(lighting="neutral"))
    except RenderUnavailable:
        await witness.render("candidate", RenderOptions(lighting="neutral"))
    reply = await witness.responses.create(model="fixture", input="image")
    return reply.output_text
"""
        if case == "caught_missing_render_sync":
            code = (
                code.replace(
                    "async def generate(image, seed, witness):", "def generate(image, seed, witness, renderer):"
                )
                .replace("await witness.render", "renderer.render")
                .replace("await witness.responses.create", "witness.responses.create")
            )
        elif case == "caught_missing_render_output_mismatch":
            expected = "different saved output"
    elif case == "render_rejection":
        code = "from renderer_client import RenderRejected\n" + SOURCE.replace(
            '    await witness.render("candidate", RenderOptions(lighting="neutral"))',
            '    try:\n        await witness.render("candidate", RenderOptions(lighting="neutral"))\n'
            '    except RenderRejected:\n        print("saved render rejection", flush=True)',
        )
    elif case in ("http", "provider", "capture", "uncaptured", "validation", "bad_request"):
        code = "import openai\n" + SOURCE.replace(
            '    reply = await witness.responses.create(model="fixture", input="image")',
            """    try:
        await witness.responses.create(model="fixture", input="image")
    except (openai.APIError, ValueError) as error:
        print("recorded failure", type(error).__name__, str(error), flush=True)
        reply = await witness.responses.create(request_id=1, model="fixture", input="image")""",
        )
        if case == "uncaptured":
            code = code.replace(
                'await witness.responses.create(model="fixture", input="image")',
                'await witness.responses.create(model="fixture", input="uncaptured")',
            )
        elif case == "validation":
            code = code.replace(
                'await witness.responses.create(model="fixture", input="image")',
                'await witness.responses.create(model="fixture", input="image", background=True)',
            )
        expected = "1"
    (source / "main.py").write_text(code)
    prompt = tmp_path / "prompts/one"
    prompt.mkdir(parents=True)
    (prompt / "image.png").write_bytes(image)
    (prompt / "expected.json").write_text(json.dumps({"available": True, "output": expected}))
    root = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/"
    preview = root + "renders/v1/91cf8613fc8ff82c97ef0240a975b1dbad2ebfcb2ad298be741fb9446b4de30d"
    previews = Stored({preview + ".png": PNG})
    artifacts = Stored({root + "submitted/one/grid.png": PNG + b"final"})
    if case in ("missing_render", "render_rejection"):
        previews.values.clear()
    if case == "render_rejection":
        previews.values[preview + ".rejection.json"] = b'{"error":"invalid_geometry"}'
    if case == "missing_final_render":
        artifacts.values.clear()
    if case == "caught_corrupt_render":
        previews.values[preview + ".png"] = b"corrupted saved PNG"

    def fetch(exchange_id: str) -> tuple[dict[str, Any], bytes]:
        capture, body = recording.fetch(exchange_id)
        return capture.model_dump(mode="json"), body

    log = Log(tmp_path)
    renderer = SavedRenderer(
        previews,
        artifacts,
        SCOPE,
        RID,
        cache=tmp_path / "preview-cache",
        artifact_cache=tmp_path / "final-cache",
    )
    try:
        result = execute_prompt(
            directory=tmp_path,
            plan={
                "prompt": "one",
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "seed": 42,
                "source_commit": "a" * 40,
                "renderer_id": RID,
            },
            renderer=renderer,
            final_render=lambda source: renderer.final_render("one", source, expected, root + "submitted/one"),
            fetch=fetch,
            log=log,
            timeout=1 if case == "timeout" else 30,
            image_index=recording.image_index,
            request_body=recording.request_body,
            lookup=recording.lookup,
        )
    finally:
        log.close()
    assert result["passed"] == (
        case
        not in (
            "request",
            "output",
            "missing_render",
            "caught_missing_render_output_mismatch",
            "caught_corrupt_render",
            "missing_final_render",
            "timeout",
        )
    )
    assert json.loads((tmp_path / "replays/one/summary.json").read_bytes())["passed"] == result["passed"]
    console = capsys.readouterr().out
    saved = (tmp_path / "console.log").read_text()
    if case not in ("none", "timeout", "missing_render", "caught_corrupt_render", "request"):
        assert "model_request" in console and "model_response" in console
    if case in ("async", "sync", "png", "tolerance", "index_miss", "render_rejection"):
        assert result["output_exact"] and result["accounting_accepted"] and result["charged_calls"] == 1
        assert result["render_calls"] == 2 and result["final_render_available"]
        assert result["historical_renders_available"]
        assert "miner stdout" in saved and "miner stderr" in saved and "native stdout" in saved
        assert "minerGenerated" in saved and "minerGenerated" not in console
        assert "return new THREE.Group()" in saved and "return new THREE.Group()" not in console
        if case != "render_rejection":
            assert (tmp_path / "replays/one/renders/000.png").read_bytes() == PNG
        assert (tmp_path / "replays/one/renders/001.png").read_bytes() == PNG + b"final"
        if case == "png":
            assert "png_request_matched" in console
            assert result["request_matches"] == {"exact": 0, "exact_png_pixels": 1, "image_tolerance": 0}
            assert result["image_tolerance_matches"] == []
        elif case == "tolerance":
            assert "image_tolerance_matched" in console
            assert result["request_matches"] == {"exact": 0, "exact_png_pixels": 0, "image_tolerance": 1}
            assert len(result["image_tolerance_matches"]) == 1
            difference = result["image_tolerance_matches"][0]["images"][0]
            assert difference["changed_pixels"] == difference["max_channel_delta"] == 1
            assert difference["total_pixels"] == 20_000
        elif case == "render_rejection":
            assert "saved render rejection" in saved
    elif case in ("caught_missing_render", "caught_missing_render_sync"):
        assert result["output_exact"] and result["accounting_accepted"] and result["final_render_available"]
        assert result["assumed_render_failures"] == [0]
        assert result["render_calls"] == 3 and result["charged_calls"] == 1
        assert "assumed_render_failure" in console
        failure = json.loads((tmp_path / "replays/one/renders/000.failure.json").read_bytes())
        assert failure["error_type"] == "RenderUnavailable"
        assert (tmp_path / "replays/one/renders/001.png").read_bytes() == PNG
        assert (tmp_path / "replays/one/renders/002.png").read_bytes() == PNG + b"final"
    elif case in ("output", "caught_missing_render_output_mismatch"):
        assert not result["output_exact"]
        assert (tmp_path / "replays/one/output.diff").is_file()
    elif case == "request":
        assert "APIConnectionError" in console and "assumed_connection_failure" in console
    elif case in ("missing_render", "missing_final_render", "caught_corrupt_render"):
        assert result["failure_type"] == ("RenderUnavailable" if case == "missing_render" else "EvidenceUnavailable")
        assert "saved render" in result["error"].lower() or "final grid" in result["error"].lower()
        if case != "missing_render":
            assert result["output_exact"]
        if case == "missing_final_render":
            assert not result["final_render_available"]
        elif case == "caught_corrupt_render":
            assert result["assumed_render_failures"] == []
    elif case == "timeout":
        assert result["timeout"]
    elif case in ("http", "provider", "capture", "uncaptured", "validation", "bad_request"):
        assert result["output_exact"] and result["accounting_accepted"]
        assert result["charged_calls"] == (2 if case == "bad_request" else 1)
        assert len(result["unknown_usage_calls"]) == (0 if case in ("validation", "bad_request") else 1)
        assert len(result["used_exchanges"]) == (1 if case in ("uncaptured", "validation") else 2)
        assert len(result["assumed_connection_failures"]) == (1 if case == "uncaptured" else 0)
        assert result["render_calls"] == 2
        expected_error = {
            "http": "RateLimitError",
            "provider": "APIError",
            "capture": "APIConnectionError",
            "uncaptured": "APIConnectionError",
            "validation": "ValueError",
            "bad_request": "BadRequestError",
        }[case]
        assert "recorded failure " + expected_error in saved
        assert "request_mismatch" not in console


def test_timeout_during_host_rpc_is_saved_and_allows_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "source").mkdir()
    worker = """import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"event": "rpc", "operation": "image_index", "request_id": 0, "session_id": None}), flush=True)
sys.stdin.readline()
"""
    popen = subprocess.Popen
    children: list[subprocess.Popen[str]] = []

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        child = popen([sys.executable, "-u", "-c", worker], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    loading: list[int] = []

    def image_index(request_id: int, session_id: str | None) -> dict[str, str | None]:
        loading.append(request_id)
        assert children[0].stdout is not None
        assert children[0].stdout.read() == ""
        return {}

    processes = ReplayProcesses()
    log = Log(tmp_path)
    try:
        renderer = SavedRenderer(
            Stored({}),
            Stored({}),
            SCOPE,
            RID,
            cache=tmp_path / "previews",
            artifact_cache=tmp_path / "finals",
        )

        def execute(prompt: dict[str, Any]) -> dict[str, Any]:
            if prompt["id"] == "two":
                return {"prompt": "two", "passed": True}
            return execute_prompt(
                directory=tmp_path,
                plan={"prompt": "one"},
                renderer=renderer,
                final_render=lambda source: PNG,
                fetch=lambda exchange_id: ({}, b""),
                image_index=image_index,
                log=log,
                timeout=1,
                processes=processes,
            )

        summary = local_replay.run_prompts(
            {"prompts": [{"id": "one"}, {"id": "two"}]},
            execute,
            "continue",
            log,
            cancel=processes.stop,
        )
    finally:
        for child in children:
            child.wait(timeout=5)
        log.close()
    assert loading == [0]
    assert summary["completed_prompts"] == 2 and not summary["passed"]
    first = summary["results"][0]
    assert first["timeout"] and not first["passed"]
    assert first["process_exit_code"] == -signal.SIGKILL
    assert first["failure_type"] == "TimeoutError" and "watchdog" in first["error"]
    assert json.loads((tmp_path / "replays/one/summary.json").read_bytes()) == first
    events = [json.loads(line)["event"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "prompt_timeout" in events and "prompt_host_error" not in events


def test_cancelling_an_exited_unreaped_worker_does_not_fail() -> None:
    processes = ReplayProcesses()
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.read() == ""
        processes.add(child)
        processes.stop()
        processes.stop()
        assert child.wait(timeout=5) == 0
    finally:
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def test_cancelling_replay_stops_active_workers_and_late_starts() -> None:
    processes = ReplayProcesses()
    children: list[subprocess.Popen[str]] = []

    def start() -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True, text=True
        )
        children.append(child)
        processes.add(child)
        return child

    try:
        first, second = start(), start()
        processes.stop()
        assert first.wait(timeout=5) < 0 and second.wait(timeout=5) < 0
        assert start().wait(timeout=5) < 0
    finally:
        for child in children:
            if child.poll() is None:
                ReplayProcesses.kill(child)
            child.wait(timeout=5)
            processes.discard(child)


@pytest.mark.parametrize("jobs", [1, 2])
def test_hotkey_round_script_prepares_and_replays_without_live_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    jobs: int,
    request: pytest.FixtureRequest,
) -> None:
    objects, files, selection = round_fixture()
    prompts = ["one", "two"][:jobs]
    source = SOURCE
    if jobs == 2:
        inputs_key = next(key for key in objects.values if key.startswith("pod-gateway/round-inputs/v1/"))
        inputs = RoundInputs.model_validate_json(objects.values[inputs_key])
        digest = hashlib.sha256(b"PNG2").hexdigest()
        extra = PromptImage(
            id="two",
            sha256=digest,
            image_key="private/competition-inputs/v1/" + digest,
            source_url="https://prompts.example/two.png",
        )
        objects.values[inputs_key] = (
            inputs.model_copy(update={"prompts": (*inputs.prompts, extra)}).model_dump_json().encode()
        )
        objects.values[extra.image_key] = b"PNG2"
        config = files["config.json"]
        assert isinstance(config, dict)
        config["prompts_per_round"] = 2
        source = SOURCE.replace('image == b"PNG"', 'image in (b"PNG", b"PNG2")')
    live = Capture(tmp_path)
    live._mode = "codex"
    key, body = live._request(0, None, {"model": "fixture", "input": "image"})
    live._capture(key, body, None)
    live._mode = "api"
    key, body = live._request(0, None, {"model": "fixture", "input": "unrelated prompt"})
    live._capture(key, body, None)
    values = {
        key: saved.data
        for key, saved in live.state.objects.items()
        if key.startswith("pod-calls/") or key.endswith(("/manifest.json", "/request.json", "/response.sse"))
    }
    objects.values.update({"private/" + key: value for key, value in values.items() if key.startswith("pod-calls/")})
    replay_values = {"private/" + key: value for key, value in values.items() if not key.startswith("pod-calls/")}
    root = f"private/rounds/{selection['round_name']}/{selection['hotkey']}/"
    objects.values[root + "submitted.json"] = canonical(
        {
            prompt: {"js": root + f"submitted/{prompt}.js", "views": root + f"submitted/{prompt}", "size": 1}
            for prompt in prompts
        }
    )
    for prompt in prompts:
        objects.values[root + f"submitted/{prompt}.js"] = b"0"
        objects.values[root + f"submitted/{prompt}/grid.png"] = PNG + prompt.encode()
    replay_values[root + "renders/v1/91cf8613fc8ff82c97ef0240a975b1dbad2ebfcb2ad298be741fb9446b4de30d.png"] = PNG
    pricing = tmp_path / "pricing.json"
    pricing.write_bytes(PRICING)

    def github(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/commits/heads/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name.endswith("prompts.txt"):
                return httpx.Response(200, text="".join(f"https://prompts.example/{p}.png\n" for p in prompts))
            return httpx.Response(200, json=files[name]) if name in files else httpx.Response(404)
        if "/git/commits/" in path:
            return httpx.Response(200, json={"sha": "b" * 40})
        assert "/zipball/" in path
        return httpx.Response(200, content=archive({"main.py": source}))

    class Git(GitHubClient):
        async def __aenter__(self) -> Self:
            self._client = httpx.AsyncClient(base_url="https://github.test", transport=httpx.MockTransport(github))
            self._owns_client = True
            return self

    journal_reads: list[str] = []
    capture_reads: list[str] = []
    preview_reads: list[str] = []
    final_reads: list[str] = []
    listings: list[str] = []
    render_listings: list[str] = []
    render_writes: list[str] = []
    r2_attempts: list[str] = []
    r2_delays: list[float] = []
    r2_recovered = False
    boto_client = boto3.client

    class Storage:
        def __init__(self, client: Any) -> None:
            self.client = client

        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal r2_recovered
            key = kwargs["Key"]
            if key.startswith("private/pod-calls/"):
                assert kwargs["Bucket"] != "replay-bucket"
                journal_reads.append(key)
            if key.endswith(("/manifest.json", "/request.json", "/response.sse")):
                assert kwargs["Bucket"] == "replay-bucket"
                capture_reads.append(key)
            if "/renders/v1/" in key:
                assert kwargs["Bucket"] == "replay-bucket"
                preview_reads.append(key)
            if key.endswith("/grid.png"):
                assert kwargs["Bucket"] != "replay-bucket"
                final_reads.append(key)
            if key.startswith("private/pod-calls/") and not r2_recovered:

                def send(request: Any) -> AWSResponse:
                    r2_attempts.append(request.url)
                    if len(r2_attempts) < 5:
                        raise ReadTimeoutError(endpoint_url=request.url)
                    data = objects.values[key]
                    return AWSResponse(
                        request.url,
                        200,
                        HTTPHeaders.from_dict({"content-length": str(len(data)), "etag": '"fixture"'}),
                        io.BytesIO(data),
                    )

                self.client._endpoint.http_session.send = send
                result: dict[str, Any] = self.client.get_object(**kwargs)
                r2_recovered = True
                return result
            values = replay_values if kwargs["Bucket"] == "replay-bucket" else objects.values
            if key not in values:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": io.BytesIO(values[key]), "ETag": '"fixture"'}

        def get_paginator(self, name: str) -> Storage:
            assert name == "list_objects_v2"
            return self

        def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["Bucket"] == "replay-bucket":
                render_listings.append(kwargs["Prefix"])
                keys = [key for key in replay_values if key.startswith(kwargs["Prefix"])]
            else:
                listings.append(kwargs["Prefix"])
                keys = objects.keys(kwargs["Prefix"])
            return [{"Contents": [{"Key": key} for key in keys]}]

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["Bucket"] == "replay-bucket" and "/renders/v2/" in kwargs["Key"]
            key = kwargs["Key"]
            if key in replay_values and kwargs.get("IfNoneMatch") == "*":
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {
                            "HTTPStatusCode": 412,
                            "RequestId": "fixture",
                            "HostId": "fixture",
                            "HTTPHeaders": {},
                            "RetryAttempts": 0,
                        },
                    },
                    "PutObject",
                )
            replay_values[key] = kwargs["Body"]
            render_writes.append(key)
            return {"ETag": '"fixture"'}

    def storage(*args: Any, **kwargs: Any) -> Storage:
        client = boto_client(*args, **{**kwargs, "endpoint_url": "https://r2.example"})
        request.addfinalizer(client.close)
        return Storage(client)

    monkeypatch.setattr("boto3.client", storage)
    monkeypatch.setattr("botocore.endpoint.time.sleep", lambda delay: r2_delays.append(delay))
    monkeypatch.setattr("subnet_common.github.GitHubClient", Git)
    monkeypatch.setattr("validation_worker.replay_evidence.RECENT_WITNESS_IMAGES", (RID,))
    monkeypatch.setattr(local_replay, "INDEX_CACHE", tmp_path / "indexes")
    for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "fixture-value")
    monkeypatch.setenv("R2_REPLAY_BUCKET", "replay-bucket")
    monkeypatch.delenv("REPLAY_RENDERER_ID", raising=False)
    monkeypatch.delenv("WITNESS_IMAGE", raising=False)
    script = Path(__file__).resolve().parents[1] / "replay.py"
    arguments = [
        str(script),
        "--round",
        "test-0",
        "--all",
        "--jobs",
        str(jobs),
        "--competition-repo",
        selection["repo"],
        "--deployment",
        selection["deployment"],
        "--pricing",
        str(pricing),
    ]
    monkeypatch.delenv("PYTHONPATH", raising=False)
    cases = [
        ("fixture-endpoint", "fixture-bucket", False, 2, 3),
        ("fixture-endpoint", "fixture-bucket", False, 0, 0),
        ("fixture-endpoint", "fixture-bucket", True, 2, 0),
        ("fixture-endpoint", "other-bucket", False, 2, 0),
        ("other-endpoint", "fixture-bucket", False, 2, 3),
    ]
    for number, (endpoint, bucket, refresh, expected_reads, expected_capture_reads) in enumerate(cases):
        monkeypatch.setenv("R2_ENDPOINT", endpoint)
        monkeypatch.setenv("R2_BUCKET", bucket)
        output = tmp_path / f"replay-{number}"
        journal_reads.clear()
        capture_reads.clear()
        listings.clear()
        render_listings.clear()
        render_writes.clear()
        preview_reads.clear()
        final_reads.clear()
        hotkey = selection["hotkey"][:10] if number % 2 == 0 else selection["hotkey"]
        monkeypatch.setattr(
            "sys.argv",
            arguments + ["--hotkey", hotkey, "--output", str(output)] + (["--refresh-index"] if refresh else []),
        )
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(script), run_name="__main__")
        assert stopped.value.code == 0
        assert r2_recovered and len(r2_attempts) == 5 and len(set(r2_attempts)) == 1
        assert len(r2_delays) == 4 and all(delay > 0 for delay in r2_delays)
        summary = json.loads((output / "summary.json").read_bytes())
        assert summary["passed"] and summary["selected_prompts"] == jobs and summary["completed_prompts"] == jobs
        assert summary["concurrency"] == jobs
        assert json.loads((output / "manifest.json").read_bytes())["hotkey"] == selection["hotkey"]
        assert (output / "source/main.py").read_text() == source
        console = capsys.readouterr().out
        saved = (output / "console.log").read_text()
        assert "model_response" in console
        assert "resp_fixture" in saved and "miner stdout" in saved
        assert "minerGenerated" in saved and "minerGenerated" not in console
        assert len(journal_reads) == expected_reads
        assert len(capture_reads) == expected_capture_reads
        assert len(listings) == (1 if expected_reads else 0)
        assert len(render_listings) == (1 if number in (0, 4) else 0)
        assert len(render_writes) == (1 if number == 0 else 0)
        assert len(preview_reads) <= jobs if number in (0, 4) else not preview_reads
        assert len(final_reads) == (jobs if number in (0, 3, 4) else 0)
        assert all(row["historical_renders_available"] and row["final_render_available"] for row in summary["results"])
        if expected_reads == 0:
            assert "request_index_reused" in console
        image = (output / "replays/one/renders/000.png").read_bytes()
        assert image == PNG
        assert (output / "replays/one/renders/001.png").read_bytes() == PNG + b"one"

    if jobs == 1:
        del objects.values[root + "submitted/one/grid.png"]
        monkeypatch.setenv("R2_BUCKET", "missing-final-bucket")
        output = tmp_path / "missing-final-grid"
        monkeypatch.setattr("sys.argv", arguments + ["--hotkey", selection["hotkey"], "--output", str(output)])
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(script), run_name="__main__")
        assert stopped.value.code == 1
        summary = json.loads((output / "summary.json").read_bytes())
        result = summary["results"][0]
        assert result["output_exact"] and not result["passed"]
        assert not result["final_render_available"] and result["failure_type"] == "EvidenceUnavailable"
        assert "Saved final grid is missing" in result["error"]
