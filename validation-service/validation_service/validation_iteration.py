from datetime import datetime

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

    for name, hotkey in jobs.items():
        if shutdown.should_stop:
            break
        pod = pods.get(name)
        if hotkey in results:
            if pod is not None:
                await delete_worker_pod(runpod, pods.pop(name))
            continue
        result, has_work = await validation_outcome(git, round_num, hotkey, ref)
        if result is not None:
            results[hotkey] = result
            await git.commit_files(
                files={
                    f"rounds/{round_num}/validation_results.json": ValidationResultsAdapter.dump_json(
                        results, indent=2
                    ).decode()
                },
                message=f"Validate round {round_num}, miner {hotkey}",
                branch=settings.github_branch,
                base_sha=ref,
            )
            logger.info(f"Published {result.verdict.value}: round={round_num}, hotkey={hotkey}")
            if pod is not None:
                await delete_worker_pod(runpod, pod)
            break
        if not has_work:
            if pod is not None:
                await delete_worker_pod(runpod, pods.pop(name))
            continue
        if not pods:
            await execute_validation_job(runpod, settings, round_num, hotkey, ref)
            break

    return state.next_stage_eta
