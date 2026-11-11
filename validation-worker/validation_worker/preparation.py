import asyncio
import hashlib
import json
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import aiofiles
import httpx
from botocore.exceptions import ClientError
from subnet_common.github import GitHubClient
from subnet_common.r2_client import R2Client
from subnet_common.witness.models import json_bytes
from subnet_common.witness.rounds import RoundInputs, repository
from witness_client.calls import request_digest
from witness_pod.records import Call

from validation_worker.inputs import ValidationInputs
from validation_worker.settings import Settings


async def read_object(r2: R2Client, bucket: str, key: str) -> bytes:
    response = await r2.client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    async with body:
        return await body.read()


async def optional_object(r2: R2Client, bucket: str, key: str) -> bytes | None:
    try:
        return await read_object(r2, bucket, key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def verify_recorded_failure(call: Call, request: bytes, status_bytes: bytes) -> None:
    status = json.loads(status_bytes)
    if (
        request_digest(request) != call.request.request_sha256
        or status.get("status") != "failed"
        or status.get("error_type") != call.failure
    ):
        raise ValueError("Recorded failure binding differs: " + call.exchange_id)


async def download(client: httpx.AsyncClient, url: str, path: Path) -> None:
    async with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        async with aiofiles.open(path, "wb") as output:
            async for chunk in response.aiter_bytes():
                await output.write(chunk)


async def load_round_inputs(inputs: ValidationInputs, r2: R2Client, settings: Settings) -> RoundInputs:
    repo = repository(settings.github_repo)
    branch = settings.witness_competition_branch or settings.github_branch
    identity = hashlib.sha256(json_bytes([repo, branch])).hexdigest()
    key = f"pod-gateway/round-inputs/v1/{identity}/{inputs.submission.round}.json"
    frozen = RoundInputs.model_validate_json(await read_object(r2, r2.bucket, key))
    if (
        frozen.repository != repo
        or frozen.branch != branch
        or frozen.round != inputs.submission.round
        or frozen.number != inputs.round_num
        or frozen.seed != inputs.seed
        or frozen.storage_prefix != inputs.witness_storage_prefix
    ):
        raise ValueError("Frozen round inputs differ from the pinned competition state")
    prompt_ids = [Path(unquote(urlsplit(url).path)).stem for url in inputs.prompts]
    if prompt_ids != [prompt.id for prompt in frozen.prompts] or set(inputs.generations) != set(prompt_ids):
        raise ValueError("Published prompts and canonical outputs differ from the frozen prompt set")
    return frozen


def extract_source(archive: Path, destination: Path) -> None:
    """Copy regular repository files without following submitted links or restoring modes."""
    with tarfile.open(archive) as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            if member.isdir():
                continue
            if not member.isfile() or len(parts) < 2 or member.name.startswith("/") or ".." in parts:
                raise ValueError("Submitted source archive contains an unsafe path or link")
            target = destination.joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.extractfile(member)
            if content is None:
                raise ValueError("Submitted source archive has no file content")
            with content, target.open("xb") as output:
                shutil.copyfileobj(content, output)


async def prepare_source(inputs: ValidationInputs, git: GitHubClient, directory: Path) -> Path:
    archive = directory / "source.tar.gz"
    await download(git.client, f"/repos/{inputs.submission.repo}/tarball/{inputs.submission.commit}", archive)
    source = directory / "source"
    await asyncio.to_thread(source.mkdir)
    await asyncio.to_thread(extract_source, archive, source)
    return source
