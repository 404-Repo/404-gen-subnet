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
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp import web
from PIL import Image
from renderer_client import RenderOptions
from replay_fixtures import PRICING, SCOPE, Capture
from subnet_common.competition.submissions import MinerSubmission
from subnet_common.competition.validation import ValidationProgress
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
from validation_worker.validation import run_validation


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
        self.saved_environment = {
            key: os.environ.get(key) for key in ("GITHUB_TOKEN", "R2_SECRET_ACCESS_KEY", "AWS_MAX_ATTEMPTS")
        }
        os.environ["AWS_MAX_ATTEMPTS"] = "1"
        self.path = f"rounds/7/{self.hotkey}/validation.json"
        self.head = self.ref
        self.blobs: dict[str, str] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, str]] = {}
        self.reads: dict[str, int] = {}
        self.failed_write = False
        self.failed_read = False
        self.stop_after_chair = True
        self.stop_after_lamp_render = False
        self.unavailable_render: str | None = None
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
        self.render_keys: dict[str, str] = {}
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
            self.render_keys[name] = render_key
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
                {
                    name: {
                        "js": self.base + f"/canonical/{name}.js",
                        "views": self.base + f"/fixture/rounds/gpt-7/{self.hotkey}/submitted/{name}",
                    }
                    for name in ("chair", "lamp", "table")
                }
            ),
        }
        private_paths = (
            str(self.private_path / "token"),
            str(self.private_path / ".ssh/id_ed25519"),
            "/root/.ssh/id_ed25519",
        )
        source = f"""
import os, resource, socket, sys
import numpy as np
from pathlib import Path
from PIL import Image
import io
from httpx import HTTPStatusError
from renderer_client import RenderRejected, RenderUnavailable
print('{{"message":"Python miner log"}}', flush=True)
os.write(1, b"native import log\\n")
def generate(image, seed, witness, renderer):
    expected_env = {{
        "PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"
    }}
    if any(os.environ.get(key) != value for key, value in expected_env.items()):
        raise RuntimeError("reproducibility environment missing")
    if sys.flags.ignore_environment or not sys.flags.safe_path or not sys.flags.no_user_site:
        raise RuntimeError("Python startup profile differs")
    if resource.getrlimit(resource.RLIMIT_AS)[0] != 4 * 1024**3 or resource.getrlimit(resource.RLIMIT_NPROC)[0] != 128:
        raise RuntimeError("sandbox resource profile differs")
    if np.ones((4, 4)).dot(np.ones((4, 4)))[0, 0] != 4:
        raise RuntimeError("NumPy calculation failed")
    os.write(1, b'{{"message":"native JSON miner log"}}\\n')
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
    except RenderUnavailable as error:
        cause = error.__cause__
        if name != "table" or not isinstance(cause, HTTPStatusError) or cause.response.status_code != 500:
            raise
    return name
""".encode()
        self.source = source
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
            if (
                len(progress.prompts) == 3
                and all(state.status == "passed" for state in progress.prompts.values())
                and not self.failed_write
            ):
                self.failed_write = True
                return web.Response(status=503)
            self.head = payload["sha"]
            return web.json_response({"object": {"sha": self.head}})
        if path.startswith("/repos/test/miner/tarball/"):
            return web.Response(body=self.archive)
        if path.startswith("/canonical/"):
            return web.Response(body=self.objects["main"][path.removeprefix("/")])
        if path.startswith("/r2/"):
            bucket, key = path.removeprefix("/r2/").split("/", 1)
            if bucket == "replay":
                if self.stop_after_chair and key == self.render_keys["chair"] + ".rejection.json":
                    self.stop_after_chair = False
                    self.shutdown.request_shutdown()
                if self.stop_after_lamp_render and key == self.render_keys["lamp"] + ".png":
                    self.stop_after_lamp_render = False
                    self.shutdown.request_shutdown()
                if key == self.unavailable_render:
                    return web.Response(status=503, text="<Error><Code>ServiceUnavailable</Code></Error>")
            if key == self.frozen.prompts[1].image_key and not self.failed_read:
                self.failed_read = True
                return web.Response(status=503, text="<Error><Code>ServiceUnavailable</Code></Error>")
            data = self.objects[bucket].get(key)
            if data is not None:
                return web.Response(body=data)
            return web.Response(status=404, text="<Error><Code>NoSuchKey</Code></Error>")
        return web.Response(status=404)

    async def test_resume_after_failed_checkpoint_and_r2_read_with_isolated_miner(self) -> None:
        final_grid = f"fixture/rounds/gpt-7/{self.hotkey}/submitted/table/grid.png"
        self.objects["main"][final_grid] = self.objects["replay"].pop(self.render_keys["table"] + ".png")
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
                self.assertEqual(first.prompts["chair"].status, "passed", first.prompts["chair"].reason)
                self.shutdown = GracefulShutdown()
                await run_validation(7, self.hotkey, "b" * 40, output, git, r2, http, self.settings, self.shutdown)
        progress = ValidationProgress.model_validate_json(self.commits[self.head][self.path])
        self.assertEqual(progress.ref, self.ref)
        self.assertEqual(
            {key: item.status for key, item in progress.prompts.items()},
            {"chair": "passed", "lamp": "passed", "table": "passed"},
        )
        self.assertTrue(self.failed_read)
        self.assertTrue(self.failed_write)
        self.assertEqual(progress.prompts["lamp"].attempts, 2)
        self.assertEqual(progress.prompts["table"].attempts, 1)
        self.assertIn("http_500", progress.prompts["table"].reason)
        self.assertEqual(self.reads["/r2/main/" + final_grid], 1)
        self.assertEqual(self.reads["/r2/main/" + self.frozen.prompts[2].image_key], 1)
        self.assertEqual(self.reads["/r2/main/" + self.frozen.prompts[0].image_key], 1)
        self.assertEqual(self.reads["/canonical/chair.js"], 1)
        self.assertEqual(self.reads["/repos/test/miner/tarball/" + "d" * 40], 2)

    async def test_missing_submission_stays_pending(self) -> None:
        self.stop_after_chair = False
        self.failed_read = True
        self.commits[self.ref][f"rounds/7/{self.hotkey}/generated.json"] = json.dumps(
            {
                "chair": {"failure_reason": "generation_failed"},
                "lamp": {"failure_reason": "not_submitted"},
                "table": {"failure_reason": "not_submitted"},
            }
        )
        source = self.source.replace(b"    return name\n", b'    return name if name == "table" else None\n')
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("submission/main.py")
            member.size = len(source)
            stream.addfile(member, io.BytesIO(source))
        self.archive = archive.getvalue()

        async with (
            GitHubClient("test/competition", token=self.secret, base_url=self.base) as git,
            R2Client("fixture", "fixture", self.base + "/r2", bucket="main") as r2,
            httpx.AsyncClient(trust_env=False) as http,
            asyncio.timeout(45),
        ):
            await run_validation(
                7, self.hotkey, self.ref, self.private_path / "validation", git, r2, http, self.settings, self.shutdown
            )

        prompts = ValidationProgress.model_validate_json(self.commits[self.head][self.path]).prompts
        self.assertEqual(
            {name: prompt.status for name, prompt in prompts.items()},
            {"chair": "passed", "lamp": "pending", "table": "pending"},
        )
        for name in ("lamp", "table"):
            self.assertIn("not_submitted", prompts[name].reason)

    async def test_witness_failures_allow_miner_fallback(self) -> None:
        self.stop_after_chair = False
        self.failed_read = True
        self.settings.witness_provider = "api"
        grid = f"fixture/rounds/gpt-7/{self.hotkey}/submitted/lamp/grid.png"
        self.objects["main"][grid] = self.objects["replay"][self.render_keys["lamp"] + ".png"]
        scope = Scope("test", "gpt-7", self.hotkey)
        live = Capture(self.private_path)
        request, body = live._request(0, None, {"model": "fixture", "input": "recorded"})
        call = live.records.assign(scope, request, "start", "pod")
        self.assertTrue(live.records.claim(scope, request, "pod", call.capture_id or ""))
        live.records.authorize(scope, request, "pod", exchange_id=call.exchange_id)
        live.records.failed(scope, request, "pod", "CaptureInterrupted", exchange_id=call.exchange_id)
        key = live.records.key(scope, request.id)
        self.objects["main"]["fixture/" + key] = live.state.objects[key].data
        capture = f"fixture/rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
        self.objects["replay"][capture + "request.json"] = body
        self.objects["replay"][capture + "failure.json"] = json_bytes(
            {"status": "failed", "error_type": "CaptureInterrupted"}
        )

        source = self.source.replace(
            b"    try:\n        renderer.render(name)",
            f"""    from openai import APIConnectionError
    from witness_client import WitnessError
    try:
        witness.responses.create(model="fixture", input="recorded" if name == "chair" else "unrecorded")
    except APIConnectionError as error:
        cause = error.__cause__
        expected = ("capture_failed", {call.exchange_id!r}) if name == "chair" else ("capture_unrecorded", None)
        if (
            not isinstance(cause, WitnessError)
            or (cause.code, cause.exchange_id) != expected
            or cause.source != "gateway"
        ):
            raise
        if name != "chair":
            return name if name == "lamp" else "different output"
    else:
        raise AssertionError("Expected a Witness connection error")
    try:
        renderer.render(name)""".encode(),
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("submission/main.py")
            member.size = len(source)
            stream.addfile(member, io.BytesIO(source))
        self.archive = archive.getvalue()

        with tempfile.TemporaryDirectory() as temporary:
            async with (
                GitHubClient("test/competition", token=self.secret, base_url=self.base) as git,
                R2Client("fixture", "fixture", self.base + "/r2", bucket="main") as r2,
                httpx.AsyncClient(trust_env=False) as http,
                asyncio.timeout(45),
            ):
                await run_validation(
                    7, self.hotkey, self.ref, Path(temporary), git, r2, http, self.settings, self.shutdown
                )

        prompts = ValidationProgress.model_validate_json(self.commits[self.head][self.path]).prompts
        recorded = prompts["chair"]
        self.assertEqual((recorded.status, recorded.attempts, recorded.charges), ("passed", 1, {}), recorded.reason)
        self.assertEqual(prompts["lamp"].status, "passed", prompts["lamp"].reason)
        self.assertIn("assumed Witness connection failures", prompts["lamp"].reason)
        self.assertEqual((prompts["lamp"].consumed_calls, prompts["lamp"].consumed_credits), (0, Decimal(0)))
        self.assertEqual(self.reads["/r2/main/" + grid], 1)
        self.assertEqual(prompts["table"].status, "pending", prompts["table"].reason)
        self.assertIn("unrecorded Witness call", prompts["table"].reason)

    async def test_exhausted_retries_preserve_charges_across_restart(self) -> None:
        self.stop_after_chair = False
        self.stop_after_lamp_render = True
        self.failed_read = True
        self.settings.witness_provider = "api"
        self.objects["main"]["pod-pricing/v1/test.json"] = PRICING
        self.source = self.source.replace(
            b"    try:\n        renderer.render(name)",
            b'    if name == "lamp":\n        witness.responses.create(model="fixture", input="image")\n'
            b"    try:\n        renderer.render(name)",
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("submission/main.py")
            member.size = len(self.source)
            stream.addfile(member, io.BytesIO(self.source))
        self.archive = archive.getvalue()
        # Exhaust infrastructure retries after a successfully verified/charged call.
        self.unavailable_render = self.render_keys["lamp"] + ".png"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            live = Capture(directory)
            request, body = live._request(0, None, {"model": "fixture", "input": "image"})
            live._capture(request, body, None)
            record = live.records.load(SCOPE, request.id)
            self.assertIsNotNone(record)
            if record is None:
                self.fail("capture fixture has no recorded call")
            parameters = directory / "parameters"
            parameters.mkdir()
            (parameters / "verifying.bin").write_bytes(b"fixture proof parameters")
            scope = Scope("test", "gpt-7", self.hotkey)
            original = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{record.call.exchange_id}/"
            destination = f"fixture/rounds/{scope.round}/{scope.hotkey}/{record.call.exchange_id}/"
            manifest = json.loads(live.state.objects[original + "manifest.json"].data)
            manifest.update(round=scope.round, hotkey=scope.hotkey)
            manifest["proof_key_sha256"] = hashlib.sha256(b"fixture proof parameters").hexdigest()
            manifest_bytes = json_bytes(manifest)
            live.state.put(original + "manifest.json", manifest_bytes)
            record.scope_key = scope.key
            record.call.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            self.objects["main"]["fixture/" + live.records.key(scope, request.id)] = record.model_dump_json().encode()
            for key, snapshot in live.state.objects.items():
                if key.startswith(original):
                    self.objects["replay"][destination + key.removeprefix(original)] = snapshot.data
            verified = json_bytes({**manifest, "session_id": None})
            real_start = asyncio.create_subprocess_exec

            async def start(program: str, *args: Any, **kwargs: Any) -> Any:
                if program == "node":
                    return SimpleNamespace(
                        returncode=0,
                        communicate=AsyncMock(return_value=(verified, b"")),
                        wait=AsyncMock(return_value=0),
                    )
                return await real_start(program, *args, **kwargs)

            async with (
                GitHubClient(repo="test/competition", token=self.secret, base_url=self.base) as git,
                R2Client("fixture", "fixture", self.base + "/r2", bucket="main") as r2,
                httpx.AsyncClient(trust_env=False) as http,
                asyncio.timeout(90),
            ):
                with (
                    patch.object(execution, "PROOF_PARAMETERS", parameters),
                    patch.object(asyncio, "create_subprocess_exec", side_effect=start),
                ):
                    await run_validation(
                        7, self.hotkey, self.ref, directory, git, r2, http, self.settings, self.shutdown
                    )
                    interrupted = ValidationProgress.model_validate_json(self.commits[self.head][self.path])
                    first = interrupted.prompts["lamp"]
                    self.assertEqual((first.status, first.attempts, first.consumed_calls), ("pending", 1, 1))
                    self.shutdown = GracefulShutdown()
                    await run_validation(
                        7, self.hotkey, self.ref, directory, git, r2, http, self.settings, self.shutdown
                    )
                    progress = ValidationProgress.model_validate_json(self.commits[self.head][self.path])
                    state = progress.prompts["lamp"]
                    self.assertEqual(state.deadline, first.deadline)
                    self.assertEqual((state.status, state.attempts), ("pending", 3))
                    self.assertIn("ServiceUnavailable", state.reason)
                    self.assertEqual(state.charges, {request.id: Decimal("2")})
                    self.assertEqual((state.consumed_calls, state.consumed_credits), (1, Decimal("2")))
                    reads = self.reads.copy()
                    await run_validation(
                        7, self.hotkey, self.ref, directory, git, r2, http, self.settings, self.shutdown
                    )
                    resumed = ValidationProgress.model_validate_json(self.commits[self.head][self.path])
                    self.assertEqual(resumed.prompts["lamp"], state)
                    self.assertEqual(
                        self.reads["/r2/main/" + self.frozen.prompts[1].image_key],
                        reads["/r2/main/" + self.frozen.prompts[1].image_key],
                    )
                    committed = [
                        ValidationProgress.model_validate_json(files[self.path]).prompts.get("lamp")
                        for files in self.commits.values()
                        if self.path in files
                    ]
                    self.assertTrue(
                        any(item and item.attempts == 1 and item.charges == state.charges for item in committed)
                    )


def test_existing_checkpoint_keeps_completed_prompts() -> None:
    saved = {
        "ref": "a" * 40,
        "prompts": {"chair": {"verdict": "passed"}, "lamp": {"verdict": "failed", "reason": "Output differs"}},
    }
    progress = ValidationProgress.model_validate_json(json.dumps(saved))
    assert progress.prompts["chair"].status == "passed"
    assert progress.prompts["lamp"].status == "failed"
    assert progress.prompts["lamp"].reason == "Output differs"


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
