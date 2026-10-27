"""Collection of generated and submitted result records from public storage."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import time_machine
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient

from submission_collector.download import DownloadPipeline
from submission_collector.settings import Settings


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
PROMPT = hashlib.sha256(b"prompt").hexdigest()
SOURCE = b"export default function generate(THREE) {}"
COMMIT = "c" * 40


class Collection:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.responses: dict[str, httpx.Response] = {}
        self.git = MockGitHubClient()
        self.git.files["rounds/1/prompts.txt"] = f"https://prompts.test/{PROMPT}.png"
        self.git.files["rounds/1/submissions.json"] = json.dumps(
            {
                HOTKEY: {
                    "repo": "test/model",
                    "commit": COMMIT,
                    "revealed_at_block": 150,
                    "tie_breaker": "0" * 128,
                    "round": "test-1",
                }
            }
        )
        self.root = f"rounds/test-1/{HOTKEY}/submitted/{PROMPT}"
        self.url = f"{settings.cdn_url}/rounds/test-1/{HOTKEY}/submitted.json"
        self.saved = GenerationResult(js=f"{self.root}.js", views=self.root, size=len(SOURCE))
        self.save()

    def save(self) -> None:
        self.responses[self.url] = httpx.Response(200, json={PROMPT: self.saved.model_dump()})

    async def run(self, handler=None):
        transport = httpx.MockTransport(
            handler or (lambda request: self.responses.get(str(request.url), httpx.Response(404)))
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            pipeline = DownloadPipeline(
                GitBatcher(git=self.git, base_sha="ref", branch=self.settings.github_branch),
                http_client,
                self.settings,
            )
            return await pipeline.run(CompetitionState(current_round=1, stage=RoundStage.COLLECTING), "ref")

    def result(self):
        return GenerationsAdapter.validate_json(self.git.committed[f"rounds/1/{HOTKEY}/generated.json"])[PROMPT]


@pytest.fixture
def collection(settings: Settings):
    return Collection(settings)


async def test_collects_saved_result_urls_and_size(collection: Collection) -> None:
    await collection.run()
    assert collection.result() == GenerationResult(
        js=f"{collection.settings.cdn_url}/{collection.root}.js",
        views=f"{collection.settings.cdn_url}/{collection.root}",
        size=len(SOURCE),
    )


async def test_collects_render_rejection(collection: Collection) -> None:
    collection.saved.views = None
    collection.saved.failure_reason = "render_rejected"
    collection.save()
    await collection.run()
    result = collection.result()
    assert result.js and result.views is None and result.failure_reason == "render_rejected"


@pytest.mark.parametrize("none", [True, False])
async def test_missing_and_declared_failures_are_recorded(collection: Collection, none: bool) -> None:
    if none:
        collection.saved = GenerationResult(failure_reason="generation_failed")
        collection.save()
    else:
        del collection.responses[collection.url]
    await collection.run()
    result = collection.result()
    assert result.js is None and result.views is None
    assert result.failure_reason == ("generation_failed" if none else "not_submitted")


async def test_cdn_failure_remains_retryable(collection: Collection) -> None:
    collection.responses[collection.url] = httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/generated.json" not in collection.git.committed
    collection.save()
    await collection.run()
    assert collection.result().views == f"{collection.settings.cdn_url}/{collection.root}"


@pytest.mark.parametrize(
    "retry_after,delay",
    [
        ("7", 7),
        (None, 2),
        ("invalid", 2),
    ],
)
async def test_rate_limited_collection_waits_before_retrying(
    collection: Collection, monkeypatch: pytest.MonkeyPatch, retry_after: str | None, delay: int
) -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    attempts = []

    def respond(request: httpx.Request) -> httpx.Response:
        attempts.append(datetime.now(UTC))
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after} if retry_after is not None else {})
        assert datetime.now(UTC) >= start + timedelta(seconds=delay)
        return collection.responses[str(request.url)]

    with time_machine.travel(start, tick=False) as clock:

        async def wait(seconds: float) -> None:
            clock.shift(seconds)

        monkeypatch.setattr("submission_collector.download.asyncio.sleep", wait)
        await collection.run(respond)

    assert len(attempts) == 2
    assert collection.result().views == f"{collection.settings.cdn_url}/{collection.root}"


async def test_exhausted_rate_limit_retries_leave_collection_retryable(
    collection: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = []

    def respond(request: httpx.Request) -> httpx.Response:
        attempts.append(datetime.now(UTC))
        return httpx.Response(429)

    with time_machine.travel(datetime(2026, 10, 26, tzinfo=UTC), tick=False) as clock:

        async def wait(seconds: float) -> None:
            clock.shift(seconds)

        monkeypatch.setattr("submission_collector.download.asyncio.sleep", wait)
        with pytest.raises(httpx.HTTPStatusError) as error:
            await collection.run(respond)

    assert error.value.response.status_code == 429
    assert len(attempts) == 5
    assert [later - earlier for earlier, later in zip(attempts, attempts[1:], strict=False)] == [
        timedelta(seconds=delay) for delay in (2, 4, 8, 16)
    ]
    assert f"rounds/1/{HOTKEY}/generated.json" not in collection.git.committed
    await collection.run()
    assert collection.result().views == f"{collection.settings.cdn_url}/{collection.root}"


async def test_rate_limit_wait_keeps_the_concurrency_slot(
    collection: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection.settings.max_concurrent_downloads = 1
    other_hotkey = "other-miner"
    submissions = json.loads(collection.git.files["rounds/1/submissions.json"])
    submissions[other_hotkey] = submissions[HOTKEY]
    collection.git.files["rounds/1/submissions.json"] = json.dumps(submissions)
    second_url = f"{collection.settings.cdn_url}/rounds/test-1/{other_hotkey}/submitted.json"
    collection.responses[second_url] = httpx.Response(200, json={PROMPT: collection.saved.model_dump()})
    waiting, resume = asyncio.Event(), asyncio.Event()
    requests = []
    sleep = asyncio.sleep

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(429)
        return collection.responses[str(request.url)]

    async def wait(seconds: float) -> None:
        waiting.set()
        await resume.wait()

    monkeypatch.setattr("submission_collector.download.asyncio.sleep", wait)
    task = asyncio.create_task(collection.run(respond))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        await sleep(0)
        assert len(requests) == 1
        first_url = requests[0]
    finally:
        resume.set()
        results = await asyncio.wait_for(task, timeout=1)
    assert sorted(requests) == sorted([first_url, collection.url, second_url])
    assert results[HOTKEY][PROMPT].views == f"{collection.settings.cdn_url}/{collection.root}"


async def test_128_miners_with_250_prompts_need_one_index_read_each(collection: Collection) -> None:
    hotkeys = [f"miner-{number}" for number in range(128)]
    prompts = [hashlib.sha256(str(number).encode()).hexdigest() for number in range(250)]
    admission = json.loads(collection.git.files["rounds/1/submissions.json"])[HOTKEY]
    collection.git.files["rounds/1/submissions.json"] = json.dumps({hotkey: admission for hotkey in hotkeys})
    collection.git.files["rounds/1/prompts.txt"] = "\n".join(f"https://prompts.test/{prompt}.png" for prompt in prompts)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        hotkey = request.url.path.split("/")[-2]
        root = f"rounds/test-1/{hotkey}/submitted"
        results = {
            prompt: GenerationResult(js=f"{root}/{prompt}.js", views=f"{root}/{prompt}", size=len(SOURCE)).model_dump()
            for prompt in prompts
            if (hotkey, prompt) != (hotkeys[0], prompts[-1])
        }
        return httpx.Response(200, json=results)

    results = await collection.run(respond)
    assert len(requests) == 128
    assert set(requests) == {
        f"{collection.settings.cdn_url}/rounds/test-1/{hotkey}/submitted.json" for hotkey in hotkeys
    }
    assert all(len(generations) == 250 for generations in results.values())
    assert results[hotkeys[0]][prompts[-1]].failure_reason == "not_submitted"
    assert results[hotkeys[1]][prompts[-1]] == GenerationResult(
        js=f"{collection.settings.cdn_url}/rounds/test-1/{hotkeys[1]}/submitted/{prompts[-1]}.js",
        views=f"{collection.settings.cdn_url}/rounds/test-1/{hotkeys[1]}/submitted/{prompts[-1]}",
        size=len(SOURCE),
    )


async def test_skips_already_completed_prompts(collection: Collection) -> None:
    existing = {
        PROMPT: GenerationResult(js="https://assets.test/chair.js", views="https://assets.test/chair", size=100)
    }
    collection.git.files[f"rounds/1/{HOTKEY}/generated.json"] = GenerationsAdapter.dump_json(existing).decode()
    assert await collection.run() == {HOTKEY: existing}
