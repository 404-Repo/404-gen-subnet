import asyncio
import json

import httpx
import pytest
from subnet_common.competition.generations import GenerationsAdapter
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient

from submission_collector.collection_iteration import collection_iteration
from submission_collector.download import ResultCollector
from submission_collector.settings import Settings
from tests.conftest import (
    SpyCollectFn,
    add_config,
    add_prompts,
    add_schedule,
    add_state,
    make_commitment,
    make_get_block,
    make_get_commitments,
    make_get_hotkey_owners,
    make_get_locked_alpha,
)


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


async def test_admission_accepts_source_before_prompt_configuration(
    git: MockGitHubClient,
    settings: Settings,
) -> None:
    add_state(git, stage=RoundStage.OPEN)
    add_schedule(git)
    add_config(git)
    add_prompts(
        git, ["https://prompts.test/chair.png", "https://prompts.test/table.png", "https://prompts.test/lamp.png"]
    )
    reveal = ((150, json.dumps({"repo": "owner/repo", "commit": "c" * 40})),)
    await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments({HOTKEY: reveal}),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey"}),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=SpyCollectFn(),
        settings=settings,
    )
    submission = json.loads(git.committed["rounds/1/submissions.json"])[HOTKEY]
    assert submission["repo"] == "owner/repo" and submission["commit"] == "c" * 40
    assert CompetitionState.model_validate_json(git.committed["state.json"]).stage == RoundStage.MINER_GENERATION


@pytest.mark.parametrize(
    "stage",
    [RoundStage.DUELS, RoundStage.FINALIZING, RoundStage.FINISHED, RoundStage.PAUSED],
)
async def test_unhandled_stage_returns_next_stage_eta_and_does_nothing(
    git: MockGitHubClient, settings: Settings, stage: RoundStage
) -> None:
    add_state(git, stage=stage)
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(),
        get_commitments=make_get_commitments(),
        get_hotkey_owners=make_get_hotkey_owners(),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=collect,
        settings=settings,
    )

    assert result is None
    assert git.committed == {}
    assert not collect.called


async def test_open_before_reveal_window_returns_eta(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=1)
    add_schedule(git, round_num=1, latest_reveal_block=200)
    add_config(git)
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(100),
        get_commitments=make_get_commitments(),
        get_hotkey_owners=make_get_hotkey_owners(),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=collect,
        settings=settings,
    )

    assert result is not None
    assert git.committed == {}
    assert not collect.called


async def test_open_no_submissions_transitions_to_finalizing(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=1)
    add_schedule(git, round_num=1, latest_reveal_block=200)
    add_config(git)
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments({}),
        get_hotkey_owners=make_get_hotkey_owners(),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=collect,
        settings=settings,
    )

    assert result is None
    assert not collect.called

    new_state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert new_state.stage == RoundStage.FINALIZING


async def test_miner_generation_before_deadline_returns_eta(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.MINER_GENERATION, round_num=1)
    add_schedule(git, round_num=1, generation_deadline_block=500)
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(300),
        get_commitments=make_get_commitments(),
        get_hotkey_owners=make_get_hotkey_owners(),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=collect,
        settings=settings,
    )

    assert result is not None
    assert git.committed == {}
    assert not collect.called


async def test_explicit_unbounded_generation_remains_open_with_a_null_eta(
    git: MockGitHubClient, settings: Settings,
) -> None:
    add_state(git, stage=RoundStage.MINER_GENERATION, round_num=1)
    add_schedule(git, generation_deadline_block=None)
    download = SpyCollectFn()
    for block in (300, 99_000_000):
        assert await collection_iteration(
            git=git, get_block=make_get_block(block), get_commitments=make_get_commitments(),
            get_hotkey_owners=make_get_hotkey_owners(), get_locked_alpha=make_get_locked_alpha(),
            collect_fn=download, settings=settings,
        ) is None
    assert not download.called and git.committed == {}


async def test_open_round_can_publish_an_explicit_unbounded_generation_deadline(
    git: MockGitHubClient, settings: Settings,
) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=None)
    add_config(git, prompts_per_round=1, carryover_prompts=0)
    add_prompts(git, ["https://prompts.test/chair.png"])
    download = SpyCollectFn()
    result = await collection_iteration(
        git=git, get_block=make_get_block(200),
        get_commitments=make_get_commitments({HOTKEY: make_commitment(block=150)}),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1"}), get_locked_alpha=make_get_locked_alpha(),
        collect_fn=download, settings=settings,
    )
    state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert result is None and state.stage == RoundStage.MINER_GENERATION and state.next_stage_eta is None
    assert "rounds/0/seed.json" in git.committed and "rounds/0/prompts.txt" in git.committed
    assert not download.called


async def test_miner_generation_past_deadline_transitions_to_collecting_and_calls_collect(
    git: MockGitHubClient, settings: Settings
) -> None:
    add_state(git, stage=RoundStage.MINER_GENERATION, round_num=1)
    add_schedule(git, round_num=1, generation_deadline_block=500)
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(500),
        get_commitments=make_get_commitments(),
        get_hotkey_owners=make_get_hotkey_owners(),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=collect,
        settings=settings,
    )

    assert result is None
    assert collect.called

    # First commit: MINER_GENERATION -> COLLECTING transition
    first_commit = git._commits[0]
    collecting_state = CompetitionState.model_validate_json(first_commit["files"]["state.json"])
    assert collecting_state.stage == RoundStage.COLLECTING

    # Last commit: COLLECTING -> DUELS transition
    last_commit = git._commits[-1]
    duels_state = CompetitionState.model_validate_json(last_commit["files"]["state.json"])
    assert duels_state.stage == RoundStage.DUELS


async def test_open_collects_submissions_and_transitions_to_miner_generation(
    git: MockGitHubClient, settings: Settings
) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=500)
    add_config(git, prompts_per_round=3, carryover_prompts=0)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c", "prompt_d", "prompt_e"])

    commitments = {HOTKEY: make_commitment(block=150)}
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments(commitments),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1"}),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=collect,
        settings=settings,
    )

    # Should return an ETA for the generation deadline
    assert result is not None
    assert not collect.called

    # Should commit state, seed, prompts, and submissions
    committed = git.committed
    assert "state.json" in committed
    assert "rounds/0/seed.json" in committed
    assert "rounds/0/prompts.txt" in committed
    assert "rounds/0/submissions.json" in committed

    # State should transition to MINER_GENERATION
    new_state = CompetitionState.model_validate_json(committed["state.json"])
    assert new_state.stage == RoundStage.MINER_GENERATION
    assert new_state.next_stage_eta is not None

    # Seed should be present
    seed_data = json.loads(committed["rounds/0/seed.json"])
    assert "seed" in seed_data
    assert isinstance(seed_data["seed"], int)

    # Prompts should be selected (3 as configured)
    prompts = committed["rounds/0/prompts.txt"].strip().splitlines()
    assert len(prompts) == 3

    # Submissions should contain the hotkey
    submissions = json.loads(committed["rounds/0/submissions.json"])
    assert HOTKEY in submissions
    assert submissions[HOTKEY]["repo"] == "test/model"
    assert submissions[HOTKEY]["commit"] == "a" * 40


HOTKEY_2 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


@pytest.mark.parametrize("seed,expected", [(42, [HOTKEY_2, HOTKEY]), (43, [HOTKEY, HOTKEY_2])])
async def test_open_saves_same_block_submissions_in_seed_order(
    git: MockGitHubClient, settings: Settings, monkeypatch: pytest.MonkeyPatch, seed: int, expected: list[str]
) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200)
    add_config(git, carryover_prompts=0)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c"])
    monkeypatch.setattr("submission_collector.collection_iteration.secrets.randbits", lambda _: seed)

    await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments({HOTKEY: make_commitment(), HOTKEY_2: make_commitment()}),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1", HOTKEY_2: "coldkey-2"}),
        get_locked_alpha=make_get_locked_alpha(),
        collect_fn=SpyCollectFn(),
        settings=settings,
    )

    submissions = json.loads(git.committed["rounds/0/submissions.json"])
    assert list(submissions) == expected


async def test_open_insufficient_lock_drops_submission(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=500)
    add_config(git)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c"])

    commitments = {HOTKEY: make_commitment(block=150)}
    collect = SpyCollectFn()

    result = await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments(commitments),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1"}),
        get_locked_alpha=make_get_locked_alpha({"coldkey-1": 99.0}),
        collect_fn=collect,
        settings=settings,
    )

    # The only submission is dropped, so the round finalizes with no submissions
    assert result is None
    new_state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert new_state.stage == RoundStage.FINALIZING


async def test_open_lock_requirement_scales_with_submitting_hotkeys_per_coldkey(
    git: MockGitHubClient, settings: Settings
) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=500)
    add_config(git)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c"])

    # Both hotkeys belong to one coldkey: 100 ρ covers one hotkey, but 200 is required
    commitments = {HOTKEY: make_commitment(block=150), HOTKEY_2: make_commitment(block=151)}
    collect = SpyCollectFn()

    await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments(commitments),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1", HOTKEY_2: "coldkey-1"}),
        get_locked_alpha=make_get_locked_alpha({"coldkey-1": 100.0}),
        collect_fn=collect,
        settings=settings,
    )

    new_state = CompetitionState.model_validate_json(git.committed["state.json"])
    assert new_state.stage == RoundStage.FINALIZING


async def test_open_sufficient_lock_keeps_all_coldkey_hotkeys(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=500)
    add_config(git)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c"])

    commitments = {HOTKEY: make_commitment(block=150), HOTKEY_2: make_commitment(block=151)}
    collect = SpyCollectFn()

    await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments(commitments),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1", HOTKEY_2: "coldkey-1"}),
        get_locked_alpha=make_get_locked_alpha({"coldkey-1": 200.0}),
        collect_fn=collect,
        settings=settings,
    )

    submissions = json.loads(git.committed["rounds/0/submissions.json"])
    assert set(submissions) == {HOTKEY, HOTKEY_2}


async def test_open_hotkey_not_in_metagraph_is_dropped(git: MockGitHubClient, settings: Settings) -> None:
    add_state(git, stage=RoundStage.OPEN, round_num=0)
    add_schedule(git, round_num=0, latest_reveal_block=200, generation_deadline_block=500)
    add_config(git)
    add_prompts(git, ["prompt_a", "prompt_b", "prompt_c"])

    commitments = {HOTKEY: make_commitment(block=150), HOTKEY_2: make_commitment(block=151)}
    collect = SpyCollectFn()

    await collection_iteration(
        git=git,
        get_block=make_get_block(200),
        get_commitments=make_get_commitments(commitments),
        get_hotkey_owners=make_get_hotkey_owners({HOTKEY: "coldkey-1"}),
        get_locked_alpha=make_get_locked_alpha({"coldkey-1": 100.0}),
        collect_fn=collect,
        settings=settings,
    )

    submissions = json.loads(git.committed["rounds/0/submissions.json"])
    assert set(submissions) == {HOTKEY}


async def test_collection_replaces_previously_collected_results(git: MockGitHubClient, settings: Settings) -> None:
    state = add_state(git, stage=RoundStage.COLLECTING)
    add_prompts(git, ["https://prompts.test/chair.png", "https://prompts.test/table.png"], round_num=1)
    git.files["rounds/1/submissions.json"] = json.dumps(
        {HOTKEY: dict(repo="test/model", commit="c" * 40, revealed_at_block=150, tie_breaker="0" * 128, round="test-1")}
    )
    path = f"rounds/1/{HOTKEY}/generated.json"
    git.files[path] = json.dumps(
        {"chair": {"js": "https://old.test/chair.js"}, "table": {"js": "https://old.test/table.js"}}
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"chair": {"js": "chair.js", "views": "chair"}})
    )
    batcher = GitBatcher(git=git, base_sha="ref", branch=settings.github_branch)
    async with httpx.AsyncClient(transport=transport) as client:
        await ResultCollector(batcher, client, settings).collect(state, "ref")
    await batcher.flush()

    results = GenerationsAdapter.validate_json(git.committed[path])
    assert results["chair"].js == f"{settings.cdn_url}/chair.js"
    assert results["chair"].views == f"{settings.cdn_url}/chair"
    assert results["table"].js is None
    assert results["table"].failure_reason == "not_submitted"


async def test_collection_uses_the_round_storage_prefix_and_retains_artifact_pointers(
    git: MockGitHubClient, settings: Settings,
) -> None:
    state = add_state(git, stage=RoundStage.COLLECTING)
    add_prompts(git, ["https://prompts.test/chair.png"], round_num=1)
    schedule = add_schedule(git, generation_deadline_block=None)
    schedule.witness_storage_prefix = "isolated/"
    git.files["rounds/1/schedule.json"] = schedule.model_dump_json()
    git.files["rounds/1/submissions.json"] = json.dumps(
        {HOTKEY: dict(repo="test/model", commit="c" * 40, revealed_at_block=150, tie_breaker="0" * 128, round="test-1")}
    )
    index = f"{settings.cdn_url}/isolated/rounds/test-1/{HOTKEY}/submitted.json"
    root = f"isolated/rounds/test-1/{HOTKEY}/submitted/chair"
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"chair": {"js": root + ".js", "views": root}},
    ) if str(request.url) == index else httpx.Response(404))
    batcher = GitBatcher(git=git, base_sha="ref", branch=settings.github_branch)
    async with httpx.AsyncClient(transport=transport) as client:
        await ResultCollector(batcher, client, settings).collect(state, "ref")
    await batcher.flush()
    result = GenerationsAdapter.validate_json(git.committed[f"rounds/1/{HOTKEY}/generated.json"])["chair"]
    assert result.js == f"{settings.cdn_url}/{root}.js" and result.views == f"{settings.cdn_url}/{root}"


async def test_retry_wait_allows_another_miner_to_be_collected(
    git: MockGitHubClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = add_state(git, stage=RoundStage.COLLECTING)
    add_prompts(git, ["https://prompts.test/chair.png"], round_num=1)
    submission = dict(repo="test/model", commit="c" * 40, revealed_at_block=150, tie_breaker="0" * 128, round="test-1")
    git.files["rounds/1/submissions.json"] = json.dumps({HOTKEY: submission, HOTKEY_2: submission})
    settings.max_concurrent_downloads = 1
    monkeypatch.setattr("submission_collector.download.random.shuffle", lambda hotkeys: None)
    rate_limited = asyncio.Event()
    other_fetched = asyncio.Event()
    attempt_times: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if HOTKEY in request.url.path:
            attempt_times.append(asyncio.get_running_loop().time())
            if not rate_limited.is_set():
                rate_limited.set()
                return httpx.Response(429, headers={"Retry-After": "4"})
        if HOTKEY_2 in request.url.path:
            other_fetched.set()
        return httpx.Response(200, json={"chair": {"js": "chair.js"}})

    batcher = GitBatcher(git=git, base_sha="ref", branch=settings.github_branch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        collecting = asyncio.create_task(ResultCollector(batcher, client, settings).collect(state, "ref"))
        try:
            await asyncio.wait_for(rate_limited.wait(), timeout=1)
            await asyncio.wait_for(other_fetched.wait(), timeout=1)
            assert not collecting.done()
        finally:
            await collecting
    assert attempt_times[1] - attempt_times[0] >= 4
    await batcher.flush()
    results = GenerationsAdapter.validate_json(git.committed[f"rounds/1/{HOTKEY}/generated.json"])
    assert results["chair"].js == f"{settings.cdn_url}/chair.js"
