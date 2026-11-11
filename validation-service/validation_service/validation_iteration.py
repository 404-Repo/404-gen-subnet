from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger
from subnet_common.competition.state import require_state
from subnet_common.competition.validation import ValidationResultsAdapter, get_validation_results
from subnet_common.competition.validation_requests import get_validation_requests
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown

from validation_service.execution import (
    delete_worker_pod,
    execute_validation_job,
    list_worker_pods,
    pod_has_stalled,
    pod_name,
    validation_outcome,
)
from validation_service.settings import Settings


async def run_validation_iteration(settings: Settings, shutdown: GracefulShutdown) -> datetime | None:
    async with (
        GitHubClient(repo=settings.github_repo, token=settings.github_token.get_secret_value()) as git,
        httpx.AsyncClient(
            base_url="https://api.runpod.io/v2/",
            headers={"Authorization": f"Bearer {settings.runpod_api_key.get_secret_value()}"},
            timeout=30,
            trust_env=False,
        ) as runpod,
    ):
        return await validation_iteration(git, runpod, settings, shutdown)


async def validation_iteration(
    git: GitHubClient, runpod: httpx.AsyncClient, settings: Settings, shutdown: GracefulShutdown
) -> datetime | None:
    ref = await git.get_ref_sha(ref=settings.github_branch)
    state = await require_state(git=git, ref=ref)
    round_num = state.current_round
    requests = await get_validation_requests(git=git, round_num=round_num, ref=ref)
    results = await get_validation_results(git=git, round_num=round_num, ref=ref)
    jobs = {pod_name(settings, round_num, request.hotkey): request.hotkey for request in requests}
    pods = await list_worker_pods(runpod, settings.pod_prefix)

    for name, worker_pod in list(pods.items()):
        if name not in jobs or worker_pod.status in ("EXITED", "ERROR", "TERMINATED"):
            await delete_worker_pod(runpod, pods.pop(name))

    finished: list[str] = []
    next_hotkey: str | None = None
    replacement_hotkey: str | None = None
    cutoff = datetime.now(UTC) - timedelta(minutes=45)
    for name, hotkey in jobs.items():
        if shutdown.should_stop:
            break
        pod = pods.get(name)
        if hotkey in results:
            if pod is not None:
                await delete_worker_pod(runpod, pods.pop(name))
            continue
        try:
            result, has_work = await validation_outcome(git, round_num, hotkey, ref)
            stalled = (
                has_work
                and pod is not None
                and await pod_has_stalled(git, settings.github_branch, round_num, hotkey, pod, cutoff)
            )
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(f"Could not read validation checkpoint: round={round_num}, hotkey={hotkey}, error={error}")
            break
        if result is not None:
            results[hotkey] = result
            finished.append(name)
        elif not has_work:
            if pod is not None:
                await delete_worker_pod(runpod, pods.pop(name))
        elif stalled:
            pod = pods.pop(name)
            logger.warning(f"Replacing stalled validation Pod {pod.id}: round={round_num}, hotkey={hotkey}")
            await delete_worker_pod(runpod, pod)
            replacement_hotkey = replacement_hotkey or hotkey
        elif next_hotkey is None:
            next_hotkey = hotkey

    if finished:
        await git.commit_files(
            files={
                f"rounds/{round_num}/validation_results.json": ValidationResultsAdapter.dump_json(
                    results, indent=2
                ).decode()
            },
            message=f"Publish validation results for round {round_num}",
            branch=settings.github_branch,
            base_sha=ref,
        )
        for name in finished:
            hotkey = jobs[name]
            logger.info(f"Published {results[hotkey].verdict.value}: round={round_num}, hotkey={hotkey}")
            if name in pods:
                await delete_worker_pod(runpod, pods.pop(name))

    next_hotkey = replacement_hotkey or next_hotkey
    if next_hotkey is not None and not pods and not shutdown.should_stop:
        await execute_validation_job(runpod, settings, round_num, next_hotkey, ref)

    return state.next_stage_eta
