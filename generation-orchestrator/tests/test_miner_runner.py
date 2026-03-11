from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from subnet_common.competition.audit_requests import AuditRequest
from subnet_common.competition.generation_audit import GenerationAuditOutcome
from subnet_common.competition.generations import GenerationResult, GenerationSource
from subnet_common.git_batcher import GitBatcher
from subnet_common.testing import MockGitHubClient

from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.gpu_provider import ContainerInfo, DeployedContainer, GPUProvider
from generation_orchestrator.miner_runner import MinerRunner
from generation_orchestrator.pod_tracker import PodTracker
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


def done_result(gen_time: float = 10.0) -> GenerationResult:
    return GenerationResult(glb="x", png="x", generation_time=gen_time, attempts=1, distance=0.01)


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


async def test_all_prompts_done_skips_deploy(settings: Settings) -> None:
    """When prior generations cover all prompts, no pods are deployed."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    prior = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(generated=prior))

    gpu_manager = AsyncMock()
    runner = make_runner(settings, mock_git, prompts=prompts, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is None  # generation-only mode (no audit_request)
    gpu_manager.get_healthy_pod.assert_not_called()


async def test_all_prompts_done_audit_mode(settings: Settings) -> None:
    """In audit mode, all prompts done triggers audit_generations and returns PASSED."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    prior = {"p1": done_result(), "p2": done_result()}
    submitted = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(generated=prior, submitted=submitted))

    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=[])
    gpu_manager = AsyncMock()
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationAuditOutcome.PASSED
    gpu_manager.get_healthy_pod.assert_not_called()


async def test_deploy_failure_audit_mode(settings: Settings) -> None:
    """When all pods fail to deploy in audit mode, returns REJECTED."""
    prompts = [make_prompt("p1")]

    mock_git = MockGitHubClient(files=_make_git_files(submitted={"p1": done_result()}))
    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=None)
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=[])
    runner = make_runner(settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)
    result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationAuditOutcome.REJECTED
    assert result.reason == "Failed to deploy a docker image"


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
    """Happy path: pod deploys, pipeline processes prompts, pod is cleaned up."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    submitted = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))

    deployed = make_deployed()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=deployed)
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    test_settings = settings.model_copy(
        update={"pods_per_miner": 1, "initial_pod_count": 1, "max_pod_attempts": 1, "max_prompt_attempts": 1}
    )

    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=[])
    runner = make_runner(test_settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.GenerationPipeline") as MockPipeline:

        async def fake_run(pod_id: str, tracker: PodTracker) -> None:
            coordinator = MockPipeline.call_args.kwargs["coordinator"]
            while True:
                entry, _ = coordinator.assign(pod_id)
                if entry is None:
                    break
                result = GenerationResult(glb="x", png="x", generation_time=10.0, attempts=1, distance=0.01)
                coordinator.record_result(pod_id, entry.prompt.stem, result)

        MockPipeline.return_value.run = AsyncMock(side_effect=fake_run)
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationAuditOutcome.PASSED
    assert result.checked_prompts == 2
    gpu_manager.delete_container.assert_called_once_with(deployed.info)


async def test_multiple_pods_deployed_and_cleaned_up(settings: Settings) -> None:
    """Runner starts pods_per_miner pods and cleans up all of them."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    submitted = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))

    deployed = make_deployed()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=deployed)
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    test_settings = settings.model_copy(
        update={"pods_per_miner": 2, "initial_pod_count": 2, "max_pod_attempts": 2, "max_prompt_attempts": 2}
    )

    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=[])
    runner = make_runner(test_settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.GenerationPipeline") as MockPipeline:

        async def fake_run(pod_id: str, tracker: PodTracker) -> None:
            coordinator = MockPipeline.call_args.kwargs["coordinator"]
            while True:
                entry, _ = coordinator.assign(pod_id)
                if entry is None:
                    break
                result = GenerationResult(glb="x", png="x", generation_time=10.0, attempts=1, distance=0.01)
                coordinator.record_result(pod_id, entry.prompt.stem, result)

        MockPipeline.return_value.run = AsyncMock(side_effect=fake_run)
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationAuditOutcome.PASSED
    assert gpu_manager.get_healthy_pod.call_count == 2
    assert gpu_manager.delete_container.call_count == 2


async def test_replaces_failed_worker(settings: Settings) -> None:
    """When a worker fails deploy, runner starts a replacement from the pod budget."""
    prompts = [make_prompt("p1"), make_prompt("p2")]
    submitted = {"p1": done_result(), "p2": done_result()}

    mock_git = MockGitHubClient(files=_make_git_files(submitted=submitted))

    deployed = make_deployed()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(side_effect=[deployed, None, deployed])
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    test_settings = settings.model_copy(
        update={"pods_per_miner": 2, "initial_pod_count": 1, "max_pod_attempts": 3, "max_prompt_attempts": 2}
    )

    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=[])
    runner = make_runner(test_settings, mock_git, prompts=prompts, audit_request=audit_request, gpu_manager=gpu_manager)

    with patch("generation_orchestrator.miner_runner.GenerationPipeline") as MockPipeline:

        async def fake_run(pod_id: str, tracker: PodTracker) -> None:
            await asyncio.sleep(0)  # yield so failed worker can be reaped first
            coordinator = MockPipeline.call_args.kwargs["coordinator"]
            entry, _ = coordinator.assign(pod_id)
            if entry:
                result = GenerationResult(glb="x", png="x", generation_time=10.0, attempts=1, distance=0.01)
                coordinator.record_result(pod_id, entry.prompt.stem, result)

        MockPipeline.return_value.run = AsyncMock(side_effect=fake_run)
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationAuditOutcome.PASSED
    assert gpu_manager.get_healthy_pod.call_count == 3
    assert gpu_manager.delete_container.call_count == 2  # only successful deploys


async def test_cancels_remaining_workers_when_done(settings: Settings) -> None:
    """When all prompts are done, remaining workers are stopped via no_tasks."""
    prompts = [make_prompt("p1")]

    mock_git = MockGitHubClient(files=_make_git_files(submitted={"p1": done_result()}))

    deployed = make_deployed()
    stop = GenerationStop()

    gpu_manager = AsyncMock()
    gpu_manager.get_healthy_pod = AsyncMock(return_value=deployed)
    gpu_manager.delete_container = AsyncMock()
    gpu_manager.cleanup_by_prefix = AsyncMock(return_value=0)

    test_settings = settings.model_copy(
        update={"pods_per_miner": 2, "initial_pod_count": 2, "max_pod_attempts": 2, "max_prompt_attempts": 2}
    )

    audit_request = AuditRequest(hotkey=HOTKEY, critical_prompts=[])
    runner = make_runner(
        test_settings, mock_git, prompts=prompts, audit_request=audit_request, stop=stop, gpu_manager=gpu_manager
    )

    with patch("generation_orchestrator.miner_runner.GenerationPipeline") as MockPipeline:

        async def fake_run(pod_id: str, tracker: PodTracker) -> None:
            coordinator = MockPipeline.call_args.kwargs["coordinator"]
            entry, _ = coordinator.assign(pod_id)
            if entry:
                result = GenerationResult(glb="x", png="x", generation_time=10.0, attempts=1, distance=0.01)
                coordinator.record_result(pod_id, entry.prompt.stem, result)
            else:
                await stop.wait()  # an idle worker waits until canceled

        MockPipeline.return_value.run = AsyncMock(side_effect=fake_run)
        result = await runner.run()

    assert result is not None
    assert result.outcome == GenerationAuditOutcome.PASSED
    assert stop.reason == "no_tasks"
    assert gpu_manager.delete_container.call_count == 2  # both workers deployed
