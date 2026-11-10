"""Automatic capture replay and Linux sandbox/checkpoint integration."""

import asyncio
import hashlib
import io
import json
import os
import secrets
import shutil
import tarfile
import tempfile
import unittest
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from PIL import Image
from renderer_client import RenderOptions
from replay_fixtures import PRICING, SCOPE, Capture
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.r2_client import R2Client
from subnet_common.witness.models import Scope, json_bytes
from subnet_common.witness.pricing import PromptBudget
from subnet_common.witness.rounds import PromptImage, RoundInputs
from witness_pod.renderer import RenderCache

from validation_worker import execution
from validation_worker.inputs import ValidationInputs
from validation_worker.sandbox import ModelCall, encoded
from validation_worker.settings import Settings
from validation_worker.validation import ValidationProgress, run_validation


class MemoryObjects:
    def keys(self, prefix: str) -> Iterator[str]:
        return iter(())

    def get(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        return None

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        raise NotImplementedError


@unittest.skipUnless(shutil.which("nsjail") and os.environ.get("SANDBOX_ROOT"), "requires Linux sandbox runtime")
class ValidationIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ref, self.hotkey = "a" * 40, "5" * 48
        self.secret = secrets.token_hex(16)
        self.private = tempfile.TemporaryDirectory()
        self.private_path = Path(self.private.name)
        self.saved_environment = {key: os.environ.get(key) for key in ("GITHUB_TOKEN", "R2_SECRET_ACCESS_KEY")}
        self.path = f"rounds/7/{self.hotkey}/validation.json"
        self.head = self.ref
        self.blobs: dict[str, str] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, str]] = {}
        self.reads: dict[str, int] = {}
        self.failed_write = False
        self.failed_read = False
        self.stop_after_chair = True
        self.shutdown = GracefulShutdown()
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.respond)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        addresses = self.runner.addresses
        self.base = f"http://127.0.0.1:{addresses[0][1]}"
        self.port = addresses[0][1]
        self.objects: dict[str, dict[str, bytes]] = {"main": {}, "replay": {}}
        prompts = []
        for name, color in (("chair", "red"), ("lamp", "blue"), ("table", "green")):
            png = io.BytesIO()
            Image.new("RGB", (2, 2), color).save(png, format="PNG")
            data = png.getvalue()
            digest = hashlib.sha256(data).hexdigest()
            key = f"fixture/competition-inputs/v1/{digest}"
            self.objects["main"][key] = data
            prompts.append(PromptImage(id=name, source_url=f"https://unused/{name}.png", sha256=digest, image_key=key))
            self.objects["main"][f"canonical/{name}.js"] = name.encode()
            request = httpx.Request(
                "POST", "http://replay/v1/render/grid", params=RenderOptions().query(), json={"source": name}
            )
            cache = RenderCache(MemoryObjects(), Scope("test", "gpt-7", self.hotkey), storage_prefix="fixture/")
            render_key = cache.key(request.content, request.url.raw_path.decode())
            if name == "chair":
                self.objects["replay"][render_key + ".rejection.json"] = b'{"error":"render_rejected"}'
            else:
                self.objects["replay"][render_key + ".png"] = data
        self.frozen = RoundInputs(
            repository="test/competition",
            branch="main",
            revision=self.ref,
            round="gpt-7",
            number=7,
            seed=42,
            prompts=tuple(prompts),
            generation_deadline_block=100,
            storage_prefix="fixture/",
        )
        identity = hashlib.sha256(json_bytes(["test/competition", "main"])).hexdigest()
        self.objects["main"][
            f"pod-gateway/round-inputs/v1/{identity}/gpt-7.json"
        ] = self.frozen.model_dump_json().encode()
        self.objects["main"]["pod-pricing/v1/test.json"] = b'{"calls_per_prompt":8,"credits_per_prompt":20}'
        self.commits[self.ref] = {
            "rounds/7/submissions.json": json.dumps(
                {
                    self.hotkey: {
                        "repo": "test/miner",
                        "commit": "d" * 40,
                        "revealed_at_block": 1,
                        "tie_breaker": "0" * 128,
                        "round": "gpt-7",
                    }
                }
            ),
            "rounds/7/prompts.txt": "https://unused/chair.png\nhttps://unused/lamp.png\nhttps://unused/table.png\n",
            "rounds/7/seed.json": '{"seed":42}',
            "rounds/7/schedule.json": json.dumps(
                {
                    "earliest_reveal_block": 1,
                    "latest_reveal_block": 2,
                    "generation_deadline_block": 100,
                    "witness_storage_prefix": "fixture/",
                }
            ),
            f"rounds/7/{self.hotkey}/generated.json": json.dumps(
                {name: {"js": self.base + f"/canonical/{name}.js"} for name in ("chair", "lamp", "table")}
            ),
        }
        private_paths = (
            str(self.private_path / "token"),
            str(self.private_path / ".ssh/id_ed25519"),
            "/root/.ssh/id_ed25519",
        )
        source = f"""
import os, socket
from pathlib import Path
from PIL import Image
import io
from renderer_client import RenderRejected
def generate(image, seed, witness, renderer):
    scratch = Path("/scratch/previous-prompt")
    if scratch.exists():
        raise RuntimeError("previous prompt scratch survived")
    scratch.write_text("prompt state")
    for path in ("/source/main.py", "/runner.py", "/prompt.png"):
        try:
            with open(path, "ab") as stream:
                stream.write(b"modified")
        except PermissionError:
            pass
        else:
            raise RuntimeError("miner input is writable")
    if any(os.environ.get(key) for key in ("GITHUB_TOKEN", "R2_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK")):
        raise RuntimeError("credential environment exposed")
    for root in ("", "/proc/1/root", "/proc/self/root"):
        for path in {private_paths!r}:
            try:
                Path(root + path).read_bytes()
            except OSError:
                pass
            else:
                raise RuntimeError("credential file exposed")
    for environment in Path("/proc").glob("[0-9]*/environ"):
        try:
            content = environment.read_bytes()
        except OSError:
            continue
        if b"GITHUB_TOKEN=" in content or b"R2_SECRET_ACCESS_KEY=" in content:
            raise RuntimeError("worker process environment exposed")
    try:
        with socket.socket() as connection:
            connection.settimeout(0.2)
            connection.connect(("127.0.0.1", {self.port}))
    except OSError:
        pass
    else:
        raise RuntimeError("worker network exposed")
    red, green, blue = Image.open(io.BytesIO(image)).getpixel((0, 0))
    name = "chair" if red else "lamp" if blue else "table"
    try:
        renderer.render(name)
    except RenderRejected:
        pass
    return name
""".encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("submission/main.py")
            member.size = len(source)
            stream.addfile(member, io.BytesIO(source))
        self.archive = archive.getvalue()
        self.settings = Settings(
            _env_file=None,
            GITHUB_TOKEN=self.secret,
            GITHUB_REPO="test/competition",
            WITNESS_DEPLOYMENT="test",
            R2_ENDPOINT=self.base + "/r2",
            R2_BUCKET="main",
            R2_REPLAY_BUCKET="replay",
            R2_ACCESS_KEY_ID="fixture",
            R2_SECRET_ACCESS_KEY=self.secret,
            RENDERER_ID="fixture",
            SANDBOX_ROOT=os.environ["SANDBOX_ROOT"],
            MIN_CHECK_STATE_INTERVAL=1,
            SANDBOX_TIMEOUT=15,
        )

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()
        self.private.cleanup()
        for key, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def respond(self, request: web.Request) -> web.Response:
        path = request.path
        self.reads[path] = self.reads.get(path, 0) + 1
        if path.startswith("/repos/test/competition/contents/"):
            key = path.removeprefix("/repos/test/competition/contents/")
            content = self.commits[request.query["ref"]].get(key)
            return web.Response(text=content, status=200 if content is not None else 404)
        if path == "/repos/test/competition/commits/main":
            return web.json_response({"sha": self.head})
        if path.startswith("/repos/test/competition/git/commits/"):
            sha = path.rsplit("/", 1)[1]
            self.trees[sha] = self.commits[sha].copy()
            return web.json_response({"tree": {"sha": sha}})
        if path == "/repos/test/competition/git/blobs":
            payload = await request.json()
            sha = str(len(self.blobs))
            self.blobs[sha] = payload["content"]
            return web.json_response({"sha": sha})
        if path == "/repos/test/competition/git/trees":
            payload = await request.json()
            snapshot = self.trees[payload["base_tree"]].copy()
            snapshot.update({item["path"]: self.blobs[item["sha"]] for item in payload["tree"]})
            sha = f"tree-{len(self.trees)}"
            self.trees[sha] = snapshot
            return web.json_response({"sha": sha})
        if path == "/repos/test/competition/git/commits":
            payload = await request.json()
            sha = f"{len(self.commits):040x}"
            self.commits[sha] = self.trees[payload["tree"]].copy()
            return web.json_response({"sha": sha})
        if path == "/repos/test/competition/git/refs/heads/main":
            payload = await request.json()
            progress = ValidationProgress.model_validate_json(self.commits[payload["sha"]][self.path])
            if "chair" in progress.prompts and not self.failed_write:
                self.failed_write = True
                return web.Response(status=503)
            self.head = payload["sha"]
            if set(progress.prompts) == {"chair"} and self.stop_after_chair:
                self.stop_after_chair = False
                self.shutdown.request_shutdown()
            return web.json_response({"object": {"sha": self.head}})
        if path.startswith("/repos/test/miner/tarball/"):
            return web.Response(body=self.archive)
        if path.startswith("/canonical/"):
            return web.Response(body=self.objects["main"][path.removeprefix("/")])
        if path.startswith("/r2/"):
            bucket, key = path.removeprefix("/r2/").split("/", 1)
            if key == self.frozen.prompts[1].image_key and not self.failed_read:
                self.failed_read = True
                return web.Response(status=503, text="<Error><Code>ServiceUnavailable</Code></Error>")
            data = self.objects[bucket].get(key)
            if data is not None:
                return web.Response(body=data)
            return web.Response(status=404, text="<Error><Code>NoSuchKey</Code></Error>")
        return web.Response(status=404)

    async def test_resume_after_failed_checkpoint_and_r2_read_with_isolated_miner(self) -> None:
        token = self.private_path / "token"
        token.write_text(self.secret)
        key = self.private_path / ".ssh/id_ed25519"
        key.parent.mkdir(exist_ok=True)
        key.write_text("synthetic private SSH key")
        os.environ["GITHUB_TOKEN"] = self.secret
        os.environ["R2_SECRET_ACCESS_KEY"] = self.secret
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            async with (
                GitHubClient(repo="test/competition", token=self.secret, base_url=self.base) as git,
                R2Client("fixture", "fixture", self.base + "/r2", bucket="main") as r2,
                httpx.AsyncClient(trust_env=False) as http,
                asyncio.timeout(90),
            ):
                await run_validation(7, self.hotkey, self.ref, output, git, r2, http, self.settings, self.shutdown)
                first = ValidationProgress.model_validate_json(self.commits[self.head][self.path])
                self.assertEqual(set(first.prompts), {"chair"})
                self.assertEqual(first.prompts["chair"].verdict, "passed", first.prompts["chair"].reason)
                self.assertTrue(self.failed_write)
                self.shutdown = GracefulShutdown()
                await run_validation(7, self.hotkey, "b" * 40, output, git, r2, http, self.settings, self.shutdown)
        progress = ValidationProgress.model_validate_json(self.commits[self.head][self.path])
        self.assertEqual(progress.ref, self.ref)
        self.assertEqual(
            {key: item.verdict for key, item in progress.prompts.items()},
            {"chair": "passed", "lamp": "passed", "table": "passed"},
        )
        self.assertTrue(self.failed_read)
        self.assertEqual(self.reads["/r2/main/" + self.frozen.prompts[0].image_key], 1)
        self.assertEqual(self.reads["/canonical/chair.js"], 1)
        self.assertEqual(self.reads["/repos/test/miner/tarball/" + "d" * 40], 2)


def test_automatic_replay_uses_selected_pricing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = Capture(tmp_path)
    request, body = live._request(0, None, {"model": "fixture", "input": "image"})
    response = live._capture(request, body, None)
    record = live.records.load(SCOPE, request.id)
    assert record is not None
    parameters = tmp_path / "parameters"
    parameters.mkdir()
    (parameters / "verifying.bin").write_bytes(b"fixture proof parameters")
    prefix = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{record.call.exchange_id}/"
    manifest_bytes = live.state.read(prefix + "manifest.json")
    assert manifest_bytes is not None
    manifest = json.loads(manifest_bytes)
    manifest["proof_key_sha256"] = hashlib.sha256(b"fixture proof parameters").hexdigest()
    manifest_bytes = json_bytes(manifest)
    live.state.put(prefix + "manifest.json", manifest_bytes)
    record.call.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    live.state.put(live.records.key(SCOPE, request.id), record.model_dump_json().encode())

    async def read(r2: R2Client, bucket: str, key: str) -> bytes:
        data = live.state.read(key)
        assert data is not None
        return data

    verified = json_bytes({**manifest, "session_id": request.session_id})
    verifier = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(verified, b"")), wait=AsyncMock(return_value=0)
    )
    monkeypatch.setattr(execution, "PROOF_PARAMETERS", parameters)
    monkeypatch.setattr(execution, "read_object", read)
    monkeypatch.setattr(execution, "optional_object", read)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=verifier))
    secret = secrets.token_urlsafe(16)
    settings = Settings(
        _env_file=None,
        GITHUB_TOKEN=secret,
        GITHUB_REPO="fixture/competition",
        WITNESS_DEPLOYMENT=SCOPE.deployment,
        WITNESS_PROVIDER="api",
        R2_ENDPOINT="https://fixture.invalid",
        R2_BUCKET="main",
        R2_REPLAY_BUCKET="replay",
        R2_ACCESS_KEY_ID="synthetic",
        R2_SECRET_ACCESS_KEY=secret,
        RENDERER_ID="fixture",
        SANDBOX_ROOT=tmp_path,
    )
    inputs = ValidationInputs(
        round_num=0,
        hotkey=SCOPE.hotkey,
        ref="a" * 40,
        submission=MinerSubmission(
            repo="fixture/miner", commit="b" * 40, revealed_at_block=1, tie_breaker="c" * 128, round=SCOPE.round
        ),
        prompts=["one"],
        seed=42,
        generations={},
        witness_storage_prefix="",
    )
    rates = json.loads(PRICING)
    rates["models"]["fixture"].update(input_credits_per_token=0.2, output_credits_per_token=1)
    pricing = json_bytes(rates)
    budget = PromptBudget(pricing)
    result = asyncio.run(
        execution.model_response(
            ModelCall(operation="model", body=encoded(body)),
            inputs,
            R2Client("synthetic", "synthetic", settings.r2_endpoint, bucket="main"),
            settings,
            tmp_path,
            budget,
        )
    )
    assert result == (record.call.exchange_id, 200, response.body)
    budget.accept()
    assert budget.charges == {request.id: Decimal("4")}
