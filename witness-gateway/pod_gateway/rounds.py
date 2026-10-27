"""Commit-pinned generation inputs and short-lived competition admission."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

import httpx
from PIL import Image
from pydantic import Field, model_validator
from subnet_common.competition.config import require_competition_config
from subnet_common.competition.prompts import require_prompts
from subnet_common.competition.schedule import RoundSchedule, require_schedule
from subnet_common.competition.seed import require_seed_from_git
from subnet_common.competition.state import RoundStage, require_state
from subnet_common.github import GitHubClient

from .artifacts import Frozen, Objects
from .auth import AuthRejected
from .models import json_bytes
from .state import R2StateStore, StateConflict, StateStore


def repository(value: str) -> str:
    """Return a canonical GitHub repository identity."""
    if value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/").removesuffix("/")
    value = value.removesuffix(".git")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        raise ValueError("Expected a GitHub repository URL or owner/repository")
    return value.lower()


class PromptImage(Frozen):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
    source_url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_key: str


class RoundInputs(Frozen):
    repository: str
    branch: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    round: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    number: int = Field(ge=0)
    seed: int = Field(ge=0, lt=2**64)
    prompts: tuple[PromptImage, ...] = Field(min_length=1, max_length=4096)
    generation_deadline_block: int | None = Field(ge=0)
    storage_prefix: str
    save_submissions: bool = True

    @model_validator(mode="after")
    def valid_inputs(self) -> RoundInputs:
        if repository(self.repository) != self.repository:
            raise ValueError("Expected canonical competition repository")
        RoundSchedule.storage_prefix(self.storage_prefix)
        if len(self.prompt_ids) != len(self.prompts) or len({p.id for p in self.prompts}) != len(self.prompts):
            raise ValueError("Expected distinct prompt images and official IDs")
        for prompt in self.prompts:
            if prompt.image_key != f"{self.storage_prefix}competition-inputs/v1/{prompt.sha256}":
                raise ValueError("Prompt content reference differs")
        return self

    @property
    def fingerprint(self) -> str:
        # State-only commits can pause and resume the same frozen inputs.
        data = self.model_dump(mode="json", exclude={"revision", "prompts"})
        data["prompts"] = [{"id": p.id, "sha256": p.sha256, "image_key": p.image_key} for p in self.prompts]
        return hashlib.sha256(json_bytes(data)).hexdigest()

    @property
    def prompt_ids(self) -> dict[str, str]:
        return {prompt.sha256: prompt.id for prompt in self.prompts}


class RoundPolicy(Frozen):
    repository: str
    branch: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    round: str
    inputs_sha256: str | None
    stage: str
    observed_at: float
    expires_at: float
    state_expires_at: float | None = None
    block: int | None = None
    generation_deadline_block: int | None = None

    def require(self, inputs: RoundInputs, now: float, *, generation: bool = True) -> None:
        expires = self.expires_at if generation or self.state_expires_at is None else self.state_expires_at
        if not self.observed_at <= now + 5 or not now < expires <= self.observed_at + 120:
            raise AuthRejected("round_policy_stale", 503)
        if (self.repository != inputs.repository or self.branch != inputs.branch or self.round != inputs.round
                or self.inputs_sha256 != inputs.fingerprint):
            raise AuthRejected("round_policy_mismatch", 403)
        if self.generation_deadline_block != inputs.generation_deadline_block:
            raise AuthRejected("round_policy_mismatch", 403)
        if not generation:
            return
        if self.stage != RoundStage.MINER_GENERATION.value:
            raise AuthRejected("round_closed", 403)
        if inputs.generation_deadline_block is not None and (
            self.block is None or self.block >= inputs.generation_deadline_block
        ):
            raise AuthRejected("round_closed", 403)


class RoundImages:
    def __init__(self, state: R2StateStore) -> None:
        self.state = R2StateStore(state.client, state.bucket, max_bytes=8 * 1024 * 1024)

    def get(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        return self.state.read(key, limit)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        try:
            self.state.save(key, data, None)
        except StateConflict:
            if self.get(key) != data:
                raise ValueError("Stored prompt bytes differ") from None


class RoundController:
    def __init__(
        self, *, git: GitHubClient, repo: str, branch: str, objects: Objects, pins: StateStore,
        download: Callable[[str], Awaitable[bytes]], head: Callable[[], Awaitable[tuple[int, float]]],
        ttl: float = 30, clock: Callable[[], float] = time.time,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch) is None
                or ".." in branch or not 5 <= ttl <= 120):
            raise ValueError("Invalid round refresh configuration")
        self.git, self.repository, self.branch = git, repository(repo), branch
        self.objects, self.pins, self.download, self.head = objects, pins, download, head
        self.ttl, self.clock = ttl, clock
        self.on_close = on_close
        self.inputs: RoundInputs | None = None
        self.policy: RoundPolicy | None = None
        self._resolved: dict[str, RoundInputs] = {}

    def require(self, *, generation: bool = True) -> RoundInputs:
        if self.inputs is None or self.policy is None:
            raise AuthRejected("round_unavailable", 503)
        self.policy.require(self.inputs, self.clock(), generation=generation)
        return self.inputs

    def _image_url(self, value: str, revision: str) -> str:
        parsed = urlsplit(value)
        if not parsed.scheme and not parsed.netloc:
            if value.startswith("/") or any(p in (".", "..") for p in value.split("/")):
                raise ValueError("Invalid repository prompt path")
            return f"https://raw.githubusercontent.com/{self.repository}/{revision}/{quote(value, safe='/')}"
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
            raise ValueError("Prompt references must use HTTPS")
        for origin, prefix in (
            ("raw.githubusercontent.com", f"/{self.repository}/{self.branch}/"),
            ("github.com", f"/{self.repository}/blob/{self.branch}/"),
        ):
            if parsed.hostname == origin and parsed.path.lower().startswith(prefix.lower()):
                path = parsed.path[len(prefix):]
                return f"https://raw.githubusercontent.com/{self.repository}/{revision}/{path}"
        return value

    async def _resolve(self, revision: str, number: int) -> RoundInputs:
        config, schedule, seed, urls = await asyncio.gather(
            require_competition_config(self.git, revision),
            require_schedule(self.git, number, revision),
            require_seed_from_git(self.git, number, revision),
            require_prompts(self.git, number, revision),
        )
        if not 1 <= len(urls) <= 4096:
            raise ValueError("Expected round prompts")
        semaphore = asyncio.Semaphore(8)

        async def image(value: str) -> tuple[PromptImage, bytes]:
            url = self._image_url(value, revision)
            name = PurePosixPath(unquote(urlsplit(value).path)).stem
            async with semaphore:
                data = await self.download(url)
            if not data or len(data) > 8 * 1024 * 1024:
                raise ValueError("Prompt image exceeds its size limit")
            with Image.open(io.BytesIO(data)) as picture:
                picture.verify()
            digest = hashlib.sha256(data).hexdigest()
            key = f"{schedule.witness_storage_prefix}competition-inputs/v1/{digest}"
            return PromptImage(id=name, source_url=url, sha256=digest, image_key=key), data

        downloaded = await asyncio.gather(*(image(url) for url in urls))
        prompts = tuple(prompt for prompt, _ in downloaded)
        if len({p.id for p in prompts}) != len(prompts) or len({p.sha256 for p in prompts}) != len(prompts):
            raise ValueError("Round prompts must have distinct official IDs and image hashes")
        inputs = RoundInputs(
            repository=self.repository, branch=self.branch, revision=revision,
            round=f"{config.name}-{number}", number=number, seed=seed, prompts=prompts,
            generation_deadline_block=schedule.generation_deadline_block,
            storage_prefix=schedule.witness_storage_prefix, save_submissions=schedule.witness_save_submissions,
        )
        for prompt, data in downloaded:
            await asyncio.to_thread(self.objects.put, prompt.image_key, data)
        return await asyncio.to_thread(self._pin, inputs)

    def _pin_key(self, round_name: str) -> str:
        identity = hashlib.sha256(json_bytes([self.repository, self.branch])).hexdigest()
        return f"pod-gateway/round-inputs/v1/{identity}/{round_name}.json"

    def _retained(self, round_name: str) -> RoundInputs | None:
        saved = self.pins.load(self._pin_key(round_name))
        if saved is None:
            return None
        inputs = RoundInputs.model_validate_json(saved.data)
        if (inputs.repository, inputs.branch, inputs.round) != (self.repository, self.branch, round_name):
            raise ValueError("Frozen round belongs to another competition source")
        return inputs

    def _pin(self, inputs: RoundInputs) -> RoundInputs:
        key = self._pin_key(inputs.round)
        current = self.pins.load(key)
        if current is None:
            try:
                self.pins.save(key, inputs.model_dump_json().encode(), None)
                return inputs
            except StateConflict:
                current = self.pins.load(key)
        if current is None:
            raise StateConflict("Frozen round inputs are unavailable")
        retained = RoundInputs.model_validate_json(current.data)
        if retained.fingerprint != inputs.fingerprint:
            raise ValueError("Published inputs changed for an existing round; publish a new round")
        return retained

    async def refresh(self) -> None:
        observed = self.clock()
        try:
            revision = await self.git.get_ref_sha(self.branch)
            state = await require_state(self.git, revision)
            config = await require_competition_config(self.git, revision)
            round_name = f"{config.name}-{state.current_round}"
            if self.policy is not None and (
                revision != self.policy.revision or state.stage != RoundStage.MINER_GENERATION
            ):
                self.policy = None
                if self.on_close is not None:
                    await self.on_close()
            inputs = self.inputs
            block = None
            expires = observed + self.ttl
            if state.stage == RoundStage.MINER_GENERATION:
                resolved = self._resolved.get(revision)
                if resolved is None:
                    resolved = await self._resolve(revision, state.current_round)
                    # Only retain the current revision's download cache.
                    self._resolved = {revision: resolved}
                inputs = resolved
                if inputs.generation_deadline_block is not None:
                    block, block_time = await self.head()
                    if not observed - 60 <= block_time <= self.clock() + 5:
                        raise ValueError("Stale generation block")
                    # A lease never outlives the estimated block boundary. The
                    # collector's block comparison remains authoritative.
                    expires = min(expires, block_time + (inputs.generation_deadline_block - block) * 12)
            elif inputs is None or inputs.round != round_name:
                retained = await asyncio.to_thread(self._retained, round_name)
                if retained is not None:
                    inputs = retained
            policy = RoundPolicy(
                repository=self.repository, branch=self.branch, revision=revision, round=round_name,
                inputs_sha256=None if inputs is None else inputs.fingerprint, stage=state.stage.value,
                observed_at=observed, expires_at=expires, block=block,
                state_expires_at=observed + self.ttl,
                generation_deadline_block=None if inputs is None else inputs.generation_deadline_block,
            )
            self.inputs, self.policy = inputs, policy
        except Exception:
            # A newly observed but incomplete publication cannot authorize old work.
            self.policy = None
            raise


async def download_image(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("Prompt image exceeds its size limit")
        return bytes(body)
