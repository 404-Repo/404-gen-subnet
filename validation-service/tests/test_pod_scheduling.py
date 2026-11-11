import json
import secrets
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from validation_service.execution import pod_name
from validation_service.settings import Settings
from validation_service.validation_iteration import validation_iteration


class PodSchedulingTest(unittest.IsolatedAsyncioTestCase):
    async def test_finishes_known_work_after_later_checkpoint_error(self) -> None:
        token = secrets.token_hex(16)
        settings = Settings(
            _env_file=None,
            GITHUB_TOKEN=token,
            GITHUB_REPO="test/competition",
            RUNPOD_PROVIDER_API_KEY=token,
            VALIDATION_POD_PREFIX="validation-example",
            VALIDATION_POD_IMAGE="registry.example/validation-worker@sha256:" + "a" * 64,
            VALIDATION_WORKER_ENV={},
        )
        ref = "b" * 40
        results_path = "rounds/3/validation_results.json"
        candidates = ("failed", "next", "passed", "malformed", "unread")
        files = {
            "state.json": '{"current_round":3,"stage":"duels"}',
            "rounds/3/validation_requests.json": json.dumps([{"hotkey": hotkey} for hotkey in candidates]),
            "rounds/3/malformed/validation.json": "{}",
            results_path: '{"existing":{"verdict":"passed","reason":""}}',
        }
        for hotkey, status, reason in (
            ("failed", "failed", "Missing submitted source"),
            ("passed", "passed", ""),
            ("unread", "passed", ""),
        ):
            files[f"rounds/3/{hotkey}/generated.json"] = '{"prompt":{"js":"https://artifacts.test/model.js"}}'
            files[f"rounds/3/{hotkey}/validation.json"] = json.dumps(
                {"ref": ref, "prompts": {"prompt": {"status": status, "reason": reason}}}
            )
        git = AsyncMock(spec=GitHubClient)
        git.get_ref_sha.return_value = ref
        git.get_file.side_effect = lambda path, ref: files.get(path)
        git.commit_files.return_value = "c" * 40
        requests: list[httpx.Request] = []

        def runpod_response(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                pod = {"id": "finished-pod", "name": pod_name(settings, 3, "failed"), "status": "RUNNING"}
                return httpx.Response(200, json={"pods": [pod], "pagination": {"hasNextPage": False}})
            git.commit_files.assert_awaited_once()
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(
                201, json={"id": "next-pod", "name": pod_name(settings, 3, "next"), "status": "STARTING"}
            )

        async with httpx.AsyncClient(
            base_url="https://runpod.test/v2/", transport=httpx.MockTransport(runpod_response)
        ) as runpod:
            await validation_iteration(git, runpod, settings, GracefulShutdown())

        self.assertEqual(
            [(request.method, request.url.path) for request in requests],
            [("GET", "/v2/pods"), ("DELETE", "/v2/pods/finished-pod"), ("POST", "/v2/pods")],
        )
        self.assertEqual(json.loads(requests[-1].content)["name"], pod_name(settings, 3, "next"))
        published = json.loads(git.commit_files.await_args.kwargs["files"][results_path])
        self.assertEqual(
            published,
            {
                "existing": {"verdict": "passed", "reason": ""},
                "failed": {"verdict": "failed", "reason": "Missing submitted source"},
                "passed": {"verdict": "passed", "reason": ""},
            },
        )

    async def test_replaces_pod_after_45_minutes_without_checkpoint_progress(self) -> None:
        token = secrets.token_hex(16)
        settings = Settings(
            _env_file=None,
            GITHUB_TOKEN=token,
            GITHUB_REPO="test/competition",
            GITHUB_BRANCH="main",
            RUNPOD_PROVIDER_API_KEY=token,
            VALIDATION_POD_PREFIX="validation-example",
            VALIDATION_POD_IMAGE="registry.example/validation-worker@sha256:" + "a" * 64,
            VALIDATION_WORKER_ENV={},
        )
        ref = "b" * 40
        now = datetime.now(UTC)
        name = pod_name(settings, 3, "active")
        checkpoint_path = "rounds/3/active/validation.json"

        async def check_pod(pod_age: int, checkpoint_age: int | None, replace: bool) -> None:
            files = {
                "state.json": '{"current_round":3,"stage":"duels"}',
                "rounds/3/validation_requests.json": '[{"hotkey":"queued"},{"hotkey":"active"}]',
                "rounds/3/active/generated.json": '{"prompt":{"js":"https://artifacts.test/model.js"}}',
            }
            history = []
            if checkpoint_age is not None:
                files[checkpoint_path] = json.dumps(
                    {"ref": ref, "prompts": {"prompt": {"status": "pending", "attempts": 1}}}
                )
                saved_at = now - timedelta(minutes=checkpoint_age)
                history.append({"commit": {"committer": {"date": saved_at.isoformat()}}})
            git = AsyncMock(spec=GitHubClient)
            git.repo = settings.github_repo
            git.get_ref_sha.return_value = ref
            git.get_file.side_effect = lambda path, ref: files.get(path)
            history_requests: list[httpx.Request] = []
            pod_requests: list[httpx.Request] = []
            pod = {
                "id": "active-pod",
                "name": name,
                "status": "RUNNING",
                "createdAt": (now - timedelta(minutes=pod_age)).isoformat(),
            }

            def github_response(request: httpx.Request) -> httpx.Response:
                history_requests.append(request)
                self.assertEqual(request.url.path, "/repos/test/competition/commits")
                self.assertEqual(request.url.params["path"], checkpoint_path)
                self.assertEqual(request.url.params["sha"], "main")
                return httpx.Response(200, json=history)

            def runpod_response(request: httpx.Request) -> httpx.Response:
                pod_requests.append(request)
                if request.method == "GET":
                    return httpx.Response(200, json={"pods": [pod], "pagination": {"hasNextPage": False}})
                if request.method == "DELETE":
                    return httpx.Response(204)
                return httpx.Response(201, json={"id": "replacement", "name": name, "status": "STARTING"})

            async with (
                httpx.AsyncClient(
                    base_url="https://github.test", transport=httpx.MockTransport(github_response)
                ) as github,
                httpx.AsyncClient(
                    base_url="https://runpod.test/v2/", transport=httpx.MockTransport(runpod_response)
                ) as runpod,
            ):
                git.client = github
                await validation_iteration(git, runpod, settings, GracefulShutdown())

            expected = [("GET", "/v2/pods")]
            if replace:
                expected.extend([("DELETE", "/v2/pods/active-pod"), ("POST", "/v2/pods")])
            self.assertEqual([(request.method, request.url.path) for request in pod_requests], expected)
            self.assertEqual(len(history_requests), int(pod_age >= 45))
            if replace:
                launch = json.loads(pod_requests[-1].content)
                self.assertEqual(launch["name"], name)
                args = json.loads(launch["args"])["cmd"]
                self.assertEqual(args[args.index("--ref") + 1], ref)

        for pod_age, checkpoint_age, replace in ((44, None, False), (46, None, True), (60, 46, True), (60, 44, False)):
            with self.subTest(pod_age=pod_age, checkpoint_age=checkpoint_age):
                await check_pod(pod_age, checkpoint_age, replace)
