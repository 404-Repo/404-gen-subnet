import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.competition.validation import PromptProgress, ValidationProgress, ValidationResultsAdapter
from subnet_common.discord import DiscordWebhook
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.testing import MockGitHubClient

import main as service
from validation_service.discord import ValidationNotifier
from validation_service.execution import pod_name
from validation_service.settings import Settings
from validation_service.validation_iteration import validation_iteration


ROUND = 7
REF = "a" * 40
HOTKEY = "private-candidate-fixture"
PROMPTS = ("chair", "lamp", "table", "shelf")
CHECKPOINT = f"rounds/{ROUND}/{HOTKEY}/validation.json"
RESULTS = f"rounds/{ROUND}/validation_results.json"
WEBHOOK = "https://discord.test/fixture"


class ValidationNotificationsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            GITHUB_TOKEN=SecretStr("fixture-github"),
            GITHUB_REPO="test/competition",
            GITHUB_BRANCH="main",
            DISCORD_WEBHOOK_URL=SecretStr(WEBHOOK),
            RUNPOD_PROVIDER_API_KEY=SecretStr("fixture-runpod"),
            VALIDATION_POD_IMAGE="registry.test/validation@sha256:" + "b" * 64,
            VALIDATION_POD_PREFIX="validation-fixture",
            VALIDATION_WORKER_ENV={},
        )
        self.git = MockGitHubClient(
            files={
                "state.json": CompetitionState(current_round=ROUND, stage=RoundStage.DUELS).model_dump_json(),
                f"rounds/{ROUND}/validation_requests.json": json.dumps([{"hotkey": HOTKEY}]),
                f"rounds/{ROUND}/{HOTKEY}/generated.json": json.dumps({prompt: {} for prompt in PROMPTS}),
            },
            ref_sha=REF,
        )
        self.shutdown = GracefulShutdown()
        self.pod: dict[str, str] | None = None
        self.creates = 0
        self.create_status = 200
        self.webhook_status = 204
        self.embeds: list[tuple[str, str]] = []
        self.now = 0.0
        self.notifier = ValidationNotifier(DiscordWebhook(WEBHOOK))
        self.runpod = httpx.AsyncClient(
            base_url="https://runpod.test/v2/", transport=httpx.MockTransport(self._runpod_response)
        )
        self.addAsyncCleanup(self.runpod.aclose)
        self.enterContext(patch("validation_service.discord.monotonic", side_effect=lambda: self.now))

        async def deliver(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), WEBHOOK)
            embed = json.loads(request.content)["embeds"][0]
            if embed["title"].endswith(("Validation Passed", "Validation Failed")):
                self.assertIn(RESULTS, self.git.committed)
            self.embeds.append((embed["title"], embed["description"]))
            return httpx.Response(self.webhook_status)

        self.enterContext(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", new=deliver))

    def _runpod_response(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            pods = [] if self.pod is None else [self.pod]
            return httpx.Response(200, json={"pods": pods, "pagination": {"hasNextPage": False}})
        if request.method == "POST":
            if self.create_status != 200:
                return httpx.Response(self.create_status)
            body = json.loads(request.content)
            self.assertNotIn("DISCORD_WEBHOOK_URL", body["env"])
            self.creates += 1
            self.pod = {"id": f"pod-{self.creates}", "name": body["name"], "status": "RUNNING"}
            return httpx.Response(200, json=self.pod)
        self.assertEqual(request.method, "DELETE")
        self.pod = None
        return httpx.Response(204)

    def _checkpoint(self, prompts: dict[str, PromptProgress]) -> None:
        self.git.files[CHECKPOINT] = ValidationProgress(ref=REF, prompts=prompts).model_dump_json()

    async def _iterate(self) -> None:
        await validation_iteration(self.git, self.runpod, self.settings, self.shutdown, self.notifier)

    @property
    def titles(self) -> list[str]:
        return [title for title, _ in self.embeds]

    async def test_start_progress_and_batched_verdicts(self) -> None:
        self.create_status = 503
        with self.assertRaises(httpx.HTTPStatusError):
            await self._iterate()
        self.assertEqual(self.embeds, [])
        self.create_status = 200
        await self._iterate()
        self.assertEqual(self.titles, ["Round 7 Validation Started"])
        candidate = pod_name(self.settings, ROUND, HOTKEY).rpartition("-")[2]
        self.assertEqual(self.embeds[0][1], f"Candidate `{candidate}`")

        passed = PromptProgress(status="passed")
        first_checkpoint = {"chair": passed, "lamp": passed}
        self._checkpoint(first_checkpoint)
        self.now = 1199
        await self._iterate()
        self.assertEqual(len(self.embeds), 1)
        self.now = 1200
        await self._iterate()
        self.assertEqual(self.titles[-1], "Round 7 Validation Progress")
        self.assertIn("**2/4**", self.embeds[-1][1])

        self._checkpoint({**first_checkpoint, "table": passed})
        self.now = 2399
        await self._iterate()
        self.assertEqual(len(self.embeds), 2)
        self.now = 2400
        await self._iterate()
        self.assertIn("**3/4**", self.embeds[-1][1])
        self.now = 3600
        if self.pod is None:
            self.fail("Expected an active Pod to replace")
        stale = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
        self.pod["createdAt"] = stale
        self.git.repo = self.settings.github_repo

        def read_checkpoint_history(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["path"], CHECKPOINT)
            return httpx.Response(200, json=[{"commit": {"committer": {"date": stale}}}])

        async with httpx.AsyncClient(
            base_url="https://github.test", transport=httpx.MockTransport(read_checkpoint_history)
        ) as github:
            with patch.object(MockGitHubClient, "client", new=github):
                await self._iterate()
        self.assertEqual(self.creates, 2)
        self.assertEqual(self.titles, ["Round 7 Validation Started"] + ["Round 7 Validation Progress"] * 2)

        self._checkpoint(dict.fromkeys(PROMPTS, passed))
        other = "other-finished-candidate"
        next_candidate = "next-candidate"
        self.git.files[f"rounds/{ROUND}/validation_requests.json"] = json.dumps(
            [{"hotkey": candidate} for candidate in (HOTKEY, other, next_candidate)]
        )
        self.git.files[f"rounds/{ROUND}/{other}/generated.json"] = '{"chair":{}}'
        self.git.files[f"rounds/{ROUND}/{other}/validation.json"] = ValidationProgress(
            ref=REF, prompts={"chair": PromptProgress(status="failed", reason="Missing submitted source")}
        ).model_dump_json()
        with patch.object(self.git, "commit_files", side_effect=RuntimeError("Fixture Git failure")):
            with self.assertRaises(RuntimeError):
                await self._iterate()
        self.assertEqual(len(self.embeds), 3)
        self.assertEqual(self.creates, 2)

        self.webhook_status = 503
        with patch.object(self.git, "commit_files", wraps=self.git.commit_files) as commit:
            await self._iterate()
            self.assertEqual(self.creates, 3)
            self.assertEqual(
                self.titles[3:],
                ["Round 7 Validation Passed", "Round 7 Validation Failed", "Round 7 Validation Started"],
            )
            name = pod_name(self.settings, ROUND, next_candidate)
            self.assertEqual(self.pod, {"id": "pod-3", "name": name, "status": "RUNNING"})
            self.assertEqual(self.embeds[-1][1], f"Candidate `{name.rpartition('-')[2]}`")
            await self._iterate()
            commit.assert_awaited_once()
        self.assertEqual(len(self.embeds), 6)
        results = ValidationResultsAdapter.validate_json(self.git.committed[RESULTS])
        self.assertEqual(
            {key: result.verdict.value for key, result in results.items()}, {HOTKEY: "passed", other: "failed"}
        )

    async def test_blocked_alert_rearms_and_failed_verdict_omits_private_reason(self) -> None:
        exhausted = PromptProgress(attempts=3, reason="private worker detail")
        blocked = dict.fromkeys(PROMPTS, exhausted)
        self._checkpoint(blocked)
        await self._iterate()
        await self._iterate()
        self.assertEqual(self.titles, ["Round 7 Validation Blocked"])
        self.assertIn("No automatic attempts remain", self.embeds[0][1])
        self.assertNotIn(RESULTS, self.git.committed)
        self.assertEqual(self.creates, 0)

        self._checkpoint({"chair": exhausted})
        await self._iterate()
        self.assertEqual(self.creates, 1)
        self._checkpoint(blocked)
        await self._iterate()
        self.assertIsNone(self.pod)
        self.assertEqual(self.titles.count("Round 7 Validation Blocked"), 2)

        self._checkpoint({"chair": PromptProgress(status="failed", reason="private worker detail")})
        await self._iterate()
        await self._iterate()
        self.assertEqual(self.titles[-1], "Round 7 Validation Failed")
        self.assertEqual(self.titles.count("Round 7 Validation Failed"), 1)
        result = ValidationResultsAdapter.validate_json(self.git.committed[RESULTS])[HOTKEY]
        self.assertEqual(result.verdict, "failed")
        self.assertEqual(result.reason, "private worker detail")
        for _, description in self.embeds:
            self.assertNotIn(HOTKEY, description)
            self.assertNotIn(result.reason, description)

    async def test_main_suppresses_service_errors_until_recovery(self) -> None:
        outcomes = iter([RuntimeError("private cycle detail"), RuntimeError(), None, ValueError()])
        waits: list[float] = []

        async def run_iteration(
            *, settings: Settings, shutdown: GracefulShutdown, discord: ValidationNotifier | None
        ) -> datetime | None:
            self.assertIs(settings, self.settings)
            self.assertIs(shutdown, self.shutdown)
            self.assertIsInstance(discord, ValidationNotifier)
            if error := next(outcomes):
                raise error
            return None

        async def wait(**kwargs: float) -> bool:
            waits.append(kwargs["timeout"])
            if len(waits) == 4:
                self.shutdown.request_shutdown()
            return self.shutdown.should_stop

        with (
            patch.object(service, "Settings", return_value=self.settings),
            patch.object(service, "GracefulShutdown", return_value=self.shutdown),
            patch.object(service, "setup_logging"),
            patch.object(self.shutdown, "setup_signal_handlers"),
            patch.object(self.shutdown, "remove_signal_handlers"),
            patch.object(self.shutdown, "wait", new=wait),
            patch.object(service, "run_validation_iteration", new=run_iteration),
        ):
            await service.main()
        self.assertEqual(waits, [self.settings.max_check_state_interval_seconds] * 4)
        self.assertEqual(self.titles, ["Validation Service Error"] * 2)
        errors = [description.splitlines()[0] for _, description in self.embeds]
        self.assertEqual(errors, ["`RuntimeError`", "`ValueError`"])
