import hashlib
import json
from datetime import datetime
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from subnet_common.competition.generations import get_generations
from subnet_common.competition.validation import ValidationProgress, ValidationResult, ValidationVerdict
from subnet_common.github import GitHubClient

from validation_service.settings import Settings


class Pod(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: str
    name: str
    status: str
    created_at: AwareDatetime | None = Field(default=None, alias="createdAt")


PodList = TypeAdapter(list[Pod])


def pod_name(settings: Settings, round_num: int, hotkey: str) -> str:
    job = json.dumps([settings.github_repo, settings.github_branch, round_num, hotkey])
    return f"{settings.pod_prefix}-{hashlib.sha256(job.encode()).hexdigest()[:16]}"


async def list_worker_pods(runpod: httpx.AsyncClient, pod_prefix: str) -> dict[str, Pod]:
    pods = {}
    params = {"limit": "1000"}
    while True:
        response = await runpod.get("pods", params=params)
        response.raise_for_status()
        try:
            page = response.json()
            for pod in PodList.validate_python(page["pods"]):
                if pod.name.rpartition("-")[0] == pod_prefix:
                    if pod.name in pods:
                        raise RuntimeError(f"More than one validation Pod for {pod.name}")
                    pods[pod.name] = pod
            if not page["pagination"]["hasNextPage"]:
                return pods
            cursor = page["pagination"]["nextCursor"]
            if not isinstance(cursor, str) or not cursor or cursor == params.get("cursor"):
                raise ValueError("Invalid page cursor")
            params["cursor"] = cursor
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Invalid RunPod inventory response") from None


async def execute_validation_job(
    runpod: httpx.AsyncClient, settings: Settings, round_num: int, hotkey: str, ref: str
) -> None:
    env = {key: value.get_secret_value() for key, value in settings.worker_environment.items()}
    env.update(
        GITHUB_TOKEN=settings.github_token.get_secret_value(),
        GITHUB_REPO=settings.github_repo,
        GITHUB_BRANCH=settings.github_branch,
    )
    args = ["--round-num", str(round_num), "--hotkey", hotkey, "--ref", ref, "--output", "/var/lib/validation"]
    response = await runpod.post(
        "pods",
        json={
            "name": pod_name(settings, round_num, hotkey),
            "image": settings.pod_image,
            "registry": settings.pod_registry,
            "cpu": {"id": "cpu3c", "vcpuCount": 4},
            "cloud": "SECURE",
            "disk": 12,
            "env": env,
            "args": json.dumps({"cmd": args}),
            "ports": [],
        },
    )
    response.raise_for_status()
    try:
        pod = Pod.model_validate_json(response.content)
    except ValidationError:
        raise RuntimeError("Invalid RunPod create response") from None
    logger.info(f"Started validation Pod {pod.id}: round={round_num}, hotkey={hotkey}")


async def delete_worker_pod(runpod: httpx.AsyncClient, pod: Pod) -> None:
    response = await runpod.delete(f"pods/{quote(pod.id, safe='')}")
    if response.status_code != 404:
        response.raise_for_status()
    logger.info(f"Removed validation Pod {pod.id}")


async def pod_has_stalled(
    git: GitHubClient, branch: str, round_num: int, hotkey: str, pod: Pod, cutoff: datetime
) -> bool:
    if pod.created_at is None or pod.created_at > cutoff:
        return False
    response = await git.client.get(
        f"/repos/{git.repo}/commits",
        params={"sha": branch, "path": f"rounds/{round_num}/{hotkey}/validation.json", "per_page": 1},
    )
    response.raise_for_status()
    history = response.json()
    if history == []:
        return True
    try:
        committed_at = datetime.fromisoformat(history[0]["commit"]["committer"]["date"])
        return committed_at <= cutoff
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid checkpoint commit timestamp") from error


async def validation_outcome(
    git: GitHubClient, round_num: int, hotkey: str, ref: str
) -> tuple[ValidationResult | None, bool]:
    """Read the verdict and whether any prompts still have attempts available."""
    raw = await git.get_file(f"rounds/{round_num}/{hotkey}/validation.json", ref=ref)
    if raw is None:
        return None, True
    progress = ValidationProgress.model_validate_json(raw)
    expected = await get_generations(git, round_num, hotkey, ref=progress.ref)
    if not expected or progress.prompts.keys() - expected.keys():
        raise ValueError("Validation checkpoint differs from the candidate's prompts")
    for state in progress.prompts.values():
        if state.status == "failed":
            return ValidationResult(verdict=ValidationVerdict.FAILED, reason=state.reason), False
    if progress.prompts.keys() != expected.keys():
        return None, True
    if all(state.status == "passed" for state in progress.prompts.values()):
        return ValidationResult(verdict=ValidationVerdict.PASSED), False
    return None, any(
        state.status == "pending" and state.retryable and state.attempts < 3 for state in progress.prompts.values()
    )
