from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("operator_command", ROOT / "miner-runtime/run.py")
assert SPEC is not None and SPEC.loader is not None
command = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(command)
sys.path[:0] = [str(ROOT / "miner-runtime/src"), str(ROOT / "shared")]


class OperatorCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "miner"
        self.source.mkdir()
        (self.source / "main.py").write_text(
            "async def generate(image, seed, witness):\n"
            "    return image.decode() + ':' + str(seed)\n"
        )
        self.output = self.root / "leader/generated.json"
        self.rows: dict[str, dict[str, object]] = {}
        self.results: list[str] = []
        self.preparation_limits: list[int] = []
        self.call_limits: list[int] = []
        self.ids = {hashlib.sha256(value).hexdigest(): value for value in (b"first", b"second")}
        self.branch = "fixture/manual"
        self.revision = "b" * 40
        self.round_number = 2
        self.seed = 42
        self.branch_reads = 0
        self.argv = [
            "--source", str(self.source), "--proof-parameters", str(self.root / "proof"),
            "--competition-branch", self.branch,
            "--cdn", "https://fixture.test",
            "--wallet-name", "validator", "--wallet-hotkey", "test", "--wallet-path", str(self.root),
            "--output", str(self.output), "--attempts", "1",
        ]

    def invoke(self, *, incomplete: bool = False, submission_error: bool = False, missing_seed: bool = False) -> None:
        import httpx
        from miner_runtime import batch

        owner = self

        class Witness:
            def __init__(self, config, **kwargs):
                owner.preparation_limits.append(kwargs["parallel_preparations"])
                owner.call_limits.append(kwargs["parallel_calls"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get_submitted(self):
                return owner.rows

            async def submit(self, image, result):
                if submission_error:
                    raise RuntimeError("fixture submission unavailable")
                owner.results.append(result)
                key = hashlib.sha256(image).hexdigest()
                owner.rows[key] = {
                    "js": f"saved/{key}.js", "views": f"saved/{key}",
                    "failure_reason": None, "size": len(result),
                }

        async def request(transport, request):
            path = request.url.path
            if request.url.host == "api.github.com":
                prefix = "/repos/404-Repo/404-active-competition"
                if path == f"{prefix}/commits/{owner.branch}":
                    owner.branch_reads += 1
                    return httpx.Response(200, json={"sha": owner.revision})
                owner.assertEqual(request.url.params["ref"], owner.revision)
                files = {
                    "state.json": {"current_round": owner.round_number, "stage": "miner_generation"},
                    "config.json": {
                        "name": "test", "description": "Test competition",
                        "first_evaluation_date": "2026-01-01", "last_competition_date": "2026-12-31",
                        "generation_stage_minutes": 180, "win_margin": 0.07,
                        "prompts_per_round": len(owner.ids), "carryover_prompts": 0,
                    },
                    f"rounds/{owner.round_number}/seed.json": {"seed": owner.seed},
                    f"rounds/{owner.round_number}/schedule.json": {
                        "earliest_reveal_block": 1, "latest_reveal_block": 2,
                        "generation_deadline_block": None, "witness_storage_prefix": "pinned/",
                    },
                }
                filename = path.removeprefix(f"{prefix}/contents/")
                if filename == f"rounds/{owner.round_number}/prompts.txt":
                    return httpx.Response(200, text="\n".join(
                        f"images/{key}.png" if index == 0 else f"https://fixture.test/{key}.png"
                        for index, key in enumerate(owner.ids)
                    ))
                if missing_seed and filename.endswith("/seed.json"):
                    return httpx.Response(404)
                if filename in files:
                    return httpx.Response(200, json=files[filename])
            if request.url.host == "raw.githubusercontent.com":
                owner.assertTrue(path.startswith(f"/404-Repo/404-active-competition/{owner.revision}/images/"))
                return httpx.Response(200, content=owner.ids[Path(path).stem])
            if request.url.host == "fixture.test" and path.endswith(".png"):
                return httpx.Response(200, content=owner.ids[Path(path).stem])
            if path == f"/pinned/rounds/test-{owner.round_number}/fixture-key/submitted.json":
                return httpx.Response(200, json={} if incomplete else owner.rows)
            raise AssertionError(f"Unexpected HTTP request: {request.method} {request.url}")

        wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="fixture-key"))
        with (
            patch.object(httpx.AsyncHTTPTransport, "handle_async_request", request),
            patch("socket.socket.connect", side_effect=AssertionError("Live network is forbidden")),
            patch("bittensor_wallet.Wallet", return_value=wallet),
            patch.object(batch, "Wallet", return_value=wallet),
            patch.object(batch, "AsyncWitnessClient", Witness),
            patch.dict(sys.modules),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            command.main(self.argv)

    def test_source_round_export_and_saved_result_resume(self) -> None:
        self.invoke()
        original = self.output.read_bytes()
        rows = json.loads(original)
        self.assertEqual(set(rows), set(self.ids))
        self.assertEqual(set(self.results), {"first:42", "second:42"})
        self.assertEqual(self.branch_reads, 1)
        for key, value in rows.items():
            self.assertEqual(value["js"], f"https://fixture.test/saved/{key}.js")
            self.assertEqual(value["views"], f"https://fixture.test/saved/{key}")
        self.invoke()
        self.assertEqual(len(self.results), 2)
        self.assertEqual(self.output.read_bytes(), original)

    def test_branch_selects_one_committed_round_snapshot(self) -> None:
        self.round_number, self.seed = 9, 987
        self.invoke()
        self.assertEqual(set(self.results), {"first:987", "second:987"})
        self.assertEqual(set(json.loads(self.output.read_bytes())), set(self.ids))
        self.assertEqual(self.branch_reads, 1)

    def test_missing_seed_preserves_existing_output_without_generating(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous index")
        with self.assertRaisesRegex(FileNotFoundError, "Seed file not found"):
            self.invoke(missing_seed=True)
        self.assertEqual(self.results, [])
        self.assertEqual(self.output.read_bytes(), b"previous index")

    def test_incomplete_index_preserves_existing_output(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous index")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.invoke(incomplete=True)
        self.assertEqual(self.output.read_bytes(), b"previous index")

    def test_unfinished_batch_does_not_export(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "2 prompts remain"):
            self.invoke(submission_error=True)
        self.assertFalse(self.output.exists())

    def test_zero_workers_is_rejected_before_startup(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            command.main([*self.argv, "--workers", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_four_preparations_preserves_generation_and_export(self) -> None:
        self.argv.extend(["--parallel-preparations", "4"])
        self.invoke()
        self.assertEqual(self.preparation_limits, [4])
        self.assertEqual(set(json.loads(self.output.read_bytes())), set(self.ids))
        self.assertEqual(set(self.results), {"first:42", "second:42"})

    def test_zero_preparations_is_rejected_before_startup(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            command.main([*self.argv, "--parallel-preparations", "0"])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.output.exists())

    def test_recipe_runs_24_prompts_concurrently(self) -> None:
        images = [f"prompt-{index}".encode() for index in range(24)]
        self.ids = {hashlib.sha256(image).hexdigest(): image for image in images}
        (self.source / "main.py").write_text(
            "import asyncio\n"
            "active = peak = 0\n"
            "async def generate(image, seed, witness):\n"
            "    global active, peak\n"
            "    active += 1\n"
            "    peak = max(peak, active)\n"
            "    await asyncio.sleep(0)\n"
            "    active -= 1\n"
            "    return str(peak) + ':' + str(seed)\n"
        )
        self.argv.extend(["--workers", "24", "--parallel-calls", "32", "--parallel-preparations", "4"])
        self.invoke()
        self.assertEqual(self.call_limits, [32])
        self.assertEqual(self.preparation_limits, [4])
        self.assertEqual(len(self.results), 24)
        self.assertEqual(set(self.results), {"24:42"})
        self.assertEqual(set(json.loads(self.output.read_bytes())), set(self.ids))

    def test_selected_source_overrides_cached_main(self) -> None:
        async def other_generate(image, seed, witness):
            return "wrong leader"

        with patch.dict(sys.modules, {"main": SimpleNamespace(generate=other_generate)}):
            self.invoke()
        self.assertEqual(set(self.results), {"first:42", "second:42"})

    def test_missing_entrypoint_does_not_import_another_leader(self) -> None:
        (self.source / "main.py").unlink()
        other = self.root / "other-leader"
        other.mkdir()
        (other / "main.py").write_text(
            "async def generate(image, seed, witness): return 'wrong leader'\n"
        )
        with patch.object(sys, "path", [*sys.path, str(other)]):
            with self.assertRaises(FileNotFoundError):
                self.invoke()
        self.assertEqual(self.results, [])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
