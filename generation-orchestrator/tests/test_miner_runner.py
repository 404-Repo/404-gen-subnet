from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_report import GenerationReportOutcome
from subnet_common.competition.generations import GenerationResult, GenerationSource
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient

from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.gpu_provider import ContainerInfo, DeployedContainer, GPUProvider
from generation_orchestrator.miner_runner import MinerRunner
from generation_orchestrator.pod_session import BatchComplete, PodReplaceRequested, ReplaceReason
from generation_orchestrator.prompts import Prompt
from generation_orchestrator.settings import Settings
from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


FIXTURES_DIR = Path(__file__).parent / "fixtures"
HOTKEY = "5abc123def"


def _generations_path(source: GenerationSource) -> str:
    return f"rounds/1/{HOTKEY}/{source}.json"


def make_runner(
    settings: Settings,
    mock_git: MockGitHubClient,
    prompts: list[Prompt] | None = None,
    audit_request: AuditRequest | None = None,
    stop: GenerationStop | None = None,
    gpu_manager: AsyncMock | None = None,
) -> MinerRunner:
    git_batcher = GitBatcher(git=mock_git, branch="main", base_sha="abc123")
    return MinerRunner(
        settings=settings,
        semaphore=StaggeredSemaphore(value=1, delay=0),
        gpu_manager=gpu_manager or AsyncMock(),
        git_batcher=git_batcher,
        hotkey=HOTKEY,
        docker_image="registry/test:latest",
        prompts=prompts or [],
        current_round=1,
        seed=42,
        stop=stop or GenerationStop(),
        audit_request=audit_request,
    )


def make_prompt(stem: str) -> Prompt:
    return Prompt(stem=stem, url=f"https://cdn/{stem}.jpg", path=FIXTURES_DIR / "prompt1.jpg")


def done_result() -> GenerationResult:
    return GenerationResult(js="x", views="x")


def make_deployed() -> DeployedContainer:
    return DeployedContainer(
        info=ContainerInfo(name="pod", url="http://fake:10006", delete_identifier="del", provider=GPUProvider.TARGON),
    )


def _serialize_results(results: dict[str, GenerationResult]) -> str:
    return json.dumps({k: v.model_dump() for k, v in results.items()})


def _make_git_files(
    generated: dict[str, GenerationResult] | None = None,
    submitted: dict[str, GenerationResult] | None = None,
) -> dict[str, str]:
    files: dict[str, str] = {}
    if generated is not None:
        files[_generations_path(GenerationSource.GENERATED)] = _serialize_results(generated)
    if submitted is not None:
        files[_generations_path(GenerationSource.SUBMITTED)] = _serialize_results(submitted)
    return files


def _mock_pod_session(
    MockSession: MagicMock,
    *,
    run_side_effect: list[BatchComplete | PodReplaceRequested],
) -> AsyncMock:
    """Configure the PodSession mock so every instantiation yields the same session mock."""
    session = AsyncMock()
    session.run = AsyncMock(side_effect=run_side_effect)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    MockSession.return_value = session
    return session


async def test_all_prompts_done_skips_deploy(settings: Settings) -> None:
    """When prior generations cover all prompts, no pods are deployed."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    prior = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(generated=prior))

    gpu_manager = AsyncMock()
    runner = make_runner(settings, mock_git, prompts=prompts, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is None
    gpu_manager.get_healthy_pod.assert_not_called()


async def test_all_prompts_done_audit_mode(settings: Settings) -> None:
    """In audit mode, all prompts done triggers summarize_generation and returns COMPLETED."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    prior = {"p1": done_result(), "p2": done_result()}
    submitted = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(generated=prior, submitted=submitted))

    audit_request = AuditRequest(hotkey=HOTKEY)
    gpu_manager = AsyncMock()
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    gpu_manager.get_healthy_pod.assert_not_called()


async def test_deploy_failure_audit_mode(settings: Settings) -> None:
    """When all initial deploy attempts fail in audit mode, returns REJECTED."""
    settings.max_initial_deploy_attempts = 2
    prompts = [make_prompt("p1")]

    mock_git = MockGitHubClient(files=_make_git_files(submitted={"p1": done_result()}))
    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=None)
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.REJECTED
    assert result.reason == "Failed to deploy a docker image"
    assert gpu_manager.get_healthy_pod.call_count == 2


async def test_initial_deploy_retry_succeeds(settings: Settings) -> None:
    """First deploy fails but second succeeds — generation proceeds."""
    settings.max_initial_deploy_attempts = 2
    prompts = [make_prompt("p1")]
    submitted = {"p1": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))
    deployed = make_deployed()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(side_effect=[None, deployed])
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[BatchComplete(generations={"p1": done_result()}, batch_time=10.0)],
        )
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert gpu_manager.get_healthy_pod.call_count == 2


async def test_stop_during_warmup_cleans_up(settings: Settings) -> None:
    """When stop fires during pod warmup, returns None and cleans up."""
    prompts = [make_prompt("p1")]
    stop = GenerationStop()

    async def stop_and_return_none(**kwargs: object) -> None:
        stop.cancel("external")
        return None

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(side_effect=stop_and_return_none)
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    runner = make_runner(settings, MockGitHubClient(), prompts=prompts, stop=stop, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is None
    gpu_manager.cleanup_by_prefix.assert_called_once_with(f"miner-01-{HOTKEY[:10].lower()}")


async def test_successful_generation(settings: Settings) -> None:
    """Happy path: single pod deploys, session processes prompts, pod is cleaned up."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    submitted = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))

    deployed = make_deployed()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=deployed)
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[
                BatchComplete(generations={"p1": done_result(), "p2": done_result()}, batch_time=20.0),
            ],
        )
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.checked_prompts == 2
    gpu_manager.get_healthy_pod.assert_called_once()
    gpu_manager.delete_container.assert_called_once_with(deployed.info)


async def test_replacement_on_crash(settings: Settings) -> None:
    """When the pod signals unreachable mid-batch, a replacement is deployed and work continues."""
    prompts = [make_prompt("p1")]

    mock_git = MockGitHubClient(files=_make_git_files(submitted={"p1": done_result()}))

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(side_effect=[make_deployed(), make_deployed()])
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[
                PodReplaceRequested(reason=ReplaceReason.UNREACHABLE),
                BatchComplete(generations={"p1": done_result()}, batch_time=15.0),
            ],
        )
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert gpu_manager.get_healthy_pod.call_count == 2
    assert gpu_manager.delete_container.call_count == 2


async def test_replacement_on_pod_request(settings: Settings) -> None:
    """When the pod requests replacement, a new pod is deployed."""
    prompts = [make_prompt("p1")]

    mock_git = MockGitHubClient(files=_make_git_files(submitted={"p1": done_result()}))

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(side_effect=[make_deployed(), make_deployed()])
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    payload = {"why": "degraded gpu"}
    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[
                PodReplaceRequested(reason=ReplaceReason.REQUESTED, payload=payload),
                BatchComplete(generations={"p1": done_result()}, batch_time=10.0),
            ],
        )
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert gpu_manager.get_healthy_pod.call_count == 2

    # The replaced pod's payload must reach the committed pod_stats.json — that's how
    # operators see *why* a miner asked to be swapped (e.g. degraded GPU diagnostics).
    pod_stats = json.loads(mock_git.committed[f"rounds/1/{HOTKEY}/pod_stats.json"])
    requested = [s for s in pod_stats.values() if s["termination_reason"] == ReplaceReason.REQUESTED.value]
    assert len(requested) == 1
    assert requested[0]["payload"] == payload
    # Provider on every pod_stats entry — operators need to know which provider hosted each pod.
    assert all(s["provider"] == GPUProvider.TARGON.value for s in pod_stats.values())


async def test_replacement_limit_reached(settings: Settings) -> None:
    """When max replacements are exhausted, the run stops."""
    settings.max_replacements = 1
    prompts = [make_prompt("p1")]

    mock_git = MockGitHubClient()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=make_deployed())
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    runner = make_runner(settings, mock_git, prompts=prompts, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[
                PodReplaceRequested(reason=ReplaceReason.UNREACHABLE),
                PodReplaceRequested(reason=ReplaceReason.UNREACHABLE),
            ],
        )
        result = await runner.run()

    assert result is None
    assert gpu_manager.get_healthy_pod.call_count == 2


async def test_batch_time_limit_preserves_earlier_batches(settings: Settings) -> None:
    """When a batch times out mid-run, completed batches from that pod survive the swap.

    Pod 1 finishes p1 (partial-batch result: one success, one miner failure), then the next
    batch times out. Pod 2 picks up the remaining prompts. The final state has all three
    rows — proving p1 + its miner-failed sibling were persisted before the swap.
    """
    settings.max_replacements = 1
    settings.batch_size = 1
    prompts = [make_prompt("p1"), make_prompt("p2"), make_prompt("p3")]
    submitted = {"p1": done_result(), "p2": done_result(), "p3": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=make_deployed())
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    p2_failed = GenerationResult(failure_reason="miner skipped")

    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[
                # Pod 1, batch 1 (p1): partial — p1 succeeds, p2 declared a miner failure.
                BatchComplete(generations={"p1": done_result(), "p2": p2_failed}, batch_time=10.0),
                # Pod 1, batch 2 (p3): pod stalls, time limit fires.
                PodReplaceRequested(reason=ReplaceReason.BATCH_TIME_LIMIT),
                # Pod 2 picks up the one remaining prompt (p3). p1 and p2 already have rows.
                BatchComplete(generations={"p3": done_result()}, batch_time=15.0),
            ],
        )
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.checked_prompts == 3
    # p2 is a permanent miner failure, so it counts as failed in the summary.
    assert result.failed_prompts == 1
    # Earlier batch's successes AND failures both survived the swap — time accumulated too.
    assert result.generation_time == 25.0
    # Pod 2 should have been invoked with only p3 pending (p1/p2 already resolved).
    third_call = MockSession.return_value.run.await_args_list[2]
    _round, batch = third_call.args
    assert [p.stem for p in batch] == ["p3"]


async def test_total_generation_time_accumulated(settings: Settings) -> None:
    """Total generation time is accumulated across successfully completed batches."""
    settings.max_replacements = 3
    settings.batch_size = 1
    prompts = [make_prompt("p1"), make_prompt("p2"), make_prompt("p3")]
    submitted = {"p1": done_result(), "p2": done_result(), "p3": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=make_deployed())
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY)
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.PodSession") as MockSession:
        _mock_pod_session(
            MockSession,
            run_side_effect=[
                BatchComplete(generations={"p1": done_result()}, batch_time=100.0),
                PodReplaceRequested(reason=ReplaceReason.UNREACHABLE),
                BatchComplete(generations={"p2": done_result()}, batch_time=100.0),
                PodReplaceRequested(reason=ReplaceReason.UNREACHABLE),
                BatchComplete(generations={"p3": done_result()}, batch_time=50.0),
            ],
        )
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationReportOutcome.COMPLETED
    assert result.generation_time == 250.0
