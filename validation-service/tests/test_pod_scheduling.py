import json
import secrets
import unittest
from unittest.mock import AsyncMock

import httpx
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from validation_service.execution import pod_name
from validation_service.settings import Settings
from validation_service.validation_iteration import validation_iteration


class PodSchedulingTest(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_finished_results_before_starting_next_pod(self) -> None:
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
        files = {
            "state.json": '{"current_round":3,"stage":"duels"}',
            "rounds/3/validation_requests.json": '[{"hotkey":"failed"},{"hotkey":"next"},{"hotkey":"passed"}]',
            results_path: '{"existing":{"verdict":"passed","reason":""}}',
        }
        for hotkey, status, reason in (("failed", "failed", "Missing submitted source"), ("passed", "passed", "")):
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
