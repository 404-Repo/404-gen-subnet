"""Reconcile self-starting CPU jobs using the existing RunPod v2 client."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import decimal
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal
from typing import TypeVar

import httpx
from bittensor_wallet import Wallet
from loguru import logger
from miner_runtime.pod import Config, pod_body
from pod_gateway.runpod import CreateRejected, CreateUnknown, PodProvider
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.witness.models import Pod
from subnet_common.witness.rounds import repository

from .credentials import ACCESS_TOKEN_MARGIN_SECONDS, AccessCredentials
from .models import Allocation, Job
from .settings import Settings
from .store import JobStore, atomic_write


T = TypeVar("T")


async def _threaded(operation: Callable[[], T]) -> T:
    """Finish provider and journal work before propagating caller cancellation."""
    context = contextvars.copy_context()

    def invoke() -> T:
        return context.run(operation)

    future = asyncio.get_running_loop().run_in_executor(None, invoke)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


def read_config(store: JobStore, job: Job) -> Config:
    data = json.loads((store.run_directory(job) / "config.json").read_bytes())
    data.pop("credentials_url", None)
    data.pop("credential_interval_seconds", None)
    return Config(**data)


class RunpodRunner:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        provider: PodProvider,
        http: httpx.Client,
        *,
        shutdown: GracefulShutdown,
    ) -> None:
        self.settings, self.store, self.provider, self.http = settings, store, provider, http
        self.shutdown = shutdown

    def _owned(self, job: Job) -> Pod | None:
        allocation = job.allocation
        if allocation is None or allocation.phase == "terminated":
            return None
        if allocation.phase == "intent" and allocation.pod_id is None:
            return None
        if allocation.pod_id is not None:
            pod = self.provider.get(allocation.pod_id)
            if pod is None:
                allocation.phase = "terminated"
                self.store.save(job)
                return None
            if pod.name != allocation.name or pod.image != job.snapshot.generation_image:
                raise RuntimeError("Generation Pod identity differs; refusing to operate")
            return pod
        matches = [pod for pod in self.provider.list(fresh=True) if pod.name == allocation.name]
        if len(matches) != 1 or matches[0].image != job.snapshot.generation_image:
            raise RuntimeError("Pod creation remains unconfirmed; no create will be repeated")
        allocation.pod_id = matches[0].id
        allocation.phase = "active"
        job.last_progress_at = allocation.requested_at or time.time()
        job.progress_warning_sent = False
        self.store.save(job)
        logger.info("Round {} recovered generation pod {} from inventory", job.snapshot.inputs.number, matches[0].id)
        return matches[0]

    def _price(self, config: Config) -> Decimal:
        settings = self.settings
        try:
            response = self.http.get(
                "https://api.runpod.io/v2/catalog/cpus",
                params={"include": "AVAILABILITY", "product": "POD", "vcpuCount": config.vcpus},
                headers={"Authorization": f"Bearer {settings.runpod_api_key.get_secret_value()}"},
            )
            response.raise_for_status()
            cpu = next(item for item in response.json()["cpus"] if item["id"] == config.cpu_id)
            sites = [
                site for site in cpu["dataCenters"] if config.data_center is None or site["id"] == config.data_center
            ]
            if cpu["availability"] not in ("HIGH", "MEDIUM", "LOW") or not any(
                site["availability"] in ("HIGH", "MEDIUM", "LOW") for site in sites
            ):
                raise ValueError("No capacity")
            hourly = Decimal(str(cpu["price"]["securePerVcpu"])) * config.vcpus
            if (
                not hourly.is_finite()
                or hourly <= 0
                or int(cpu["ramGbPerVcpu"]) * config.vcpus * 1024 < max(8192, config.go_memory_mib + 2048)
                or not cpu["vcpu"]["min"] <= config.vcpus <= cpu["vcpu"]["max"]
            ):
                raise ValueError("Unsupported CPU shape")
            total = hourly + Decimal(config.disk_gb) * Decimal("0.10") / 730
        except (httpx.HTTPError, KeyError, ValueError, TypeError, StopIteration, decimal.InvalidOperation):
            raise RuntimeError("Could not verify RunPod CPU capacity, RAM and price") from None
        if total > settings.max_hourly_cost:
            raise RuntimeError("CPU allocation exceeds the configured hourly cost ceiling")
        return total

    def _prepare_payload(self, job: Job) -> None:
        directory = self.store.run_directory(job)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (directory / "config.json").exists():
            return
        snapshot = job.snapshot
        config = Config(
            generation_image=snapshot.generation_image,
            source_repository=f"https://github.com/{repository(snapshot.leader.repo)}",
            source_commit=snapshot.leader.commit,
            prompts={prompt.id: prompt.sha256 for prompt in snapshot.inputs.prompts},
            seed=snapshot.inputs.seed,
            hotkey=snapshot.hotkey,
            account_id=snapshot.account_id,
            witness_url=snapshot.witness_url,
            cpu_id=self.settings.cpu_id,
            vcpus=self.settings.vcpus,
            disk_gb=self.settings.disk_gb,
            data_center=self.settings.data_center,
            workers=snapshot.workers,
            parallel_calls=snapshot.parallel_calls,
            parallel_preparations=snapshot.parallel_preparations,
            prover_contexts=snapshot.prover_contexts,
            go_cpus=snapshot.go_cpus,
            go_memory_mib=snapshot.go_memory_mib,
            round_inputs_fingerprint=snapshot.inputs.fingerprint,
        )
        atomic_write(directory / "config.json", json.dumps(asdict(config)).encode())

    def _ensure(self, job: Job) -> None:
        allocation = job.allocation
        if allocation is None or allocation.phase == "terminated":
            allocation = Allocation()
            job.allocations.append(allocation)
            self.store.save(job)
        elif allocation.phase != "intent":
            raise RuntimeError("Previous generation attempt must be terminated before preparation")
        self._prepare_payload(job)

    async def ensure(self, job: Job) -> None:
        await _threaded(lambda: self._ensure(job))

    def _hotkey(self, job: Job) -> str:
        snapshot = job.snapshot
        wallet = Wallet(name=snapshot.wallet_name, hotkey=snapshot.wallet_hotkey, path=str(self.settings.wallet_path))
        if wallet.hotkey_file.is_encrypted() or wallet.hotkey.ss58_address != snapshot.hotkey:
            raise ValueError("Operator hotkey differs from the frozen job")
        path = self.settings.wallet_path / snapshot.wallet_name / "hotkeys" / snapshot.wallet_hotkey
        return base64.b64encode(path.read_bytes()).decode()

    def _start(self, job: Job, credentials: AccessCredentials, min_validity_seconds: int) -> None:
        if self.shutdown.should_stop:
            return
        if credentials.account_id != job.snapshot.account_id:
            raise RuntimeError("Provider account differs from the frozen job")
        allocation = job.allocation
        if allocation is None:
            raise RuntimeError("Generation attempt is not prepared")
        if allocation.phase != "intent":
            self._owned(job)
            return
        attempts = [allocation for recorded in self.store.jobs() for allocation in recorded.allocations]
        if any(previous.id != allocation.id and previous.phase != "terminated" for previous in attempts):
            raise RuntimeError("Previous generation attempt cleanup is not confirmed")
        now = time.time()
        if any(
            previous.requested_at is not None and now < previous.requested_at + self.settings.progress_interval_seconds
            for previous in attempts
        ):
            return
        config = read_config(self.store, job)
        self._price(config)
        hotkey = self._hotkey(job)
        if credentials.expires_at < time.time() + min_validity_seconds:
            raise RuntimeError("Access token has insufficient validity for the generation window")
        body = pod_body(config, hotkey=hotkey, access_token=credentials.access_token, name=allocation.name)
        previous_requested_at = allocation.requested_at
        allocation.phase = "creating"
        allocation.requested_at = time.time()
        self.store.save(job)
        if self.shutdown.should_stop:
            allocation.phase = "intent"
            allocation.requested_at = previous_requested_at
            self.store.save(job)
            return
        number = job.snapshot.inputs.number
        logger.info("Round {} requesting generation pod {}", number, allocation.name)
        try:
            pod = self.provider.create(body)
        except CreateRejected:
            allocation.phase = "intent"
            self.store.save(job)
            raise
        except CreateUnknown:
            logger.warning("Round {} creation unconfirmed for generation pod {}", number, allocation.name)
            raise
        if pod.name != allocation.name or pod.image != job.snapshot.generation_image:
            raise RuntimeError("RunPod create returned a different generation Pod")
        logger.info("Round {} created generation pod {}", number, pod.id)
        allocation.pod_id = pod.id
        allocation.phase = "active"
        job.last_progress_at = allocation.requested_at
        job.progress_warning_sent = False
        self.store.save(job)

    async def start(
        self, job: Job, credentials: AccessCredentials, *, min_validity_seconds: int = ACCESS_TOKEN_MARGIN_SECONDS
    ) -> None:
        await _threaded(lambda: self._start(job, credentials, min_validity_seconds))

    async def status(self, job: Job) -> dict[str, str | None]:
        pod = await _threaded(lambda: self._owned(job))
        allocation = job.allocation
        return {
            "pod_id": None if allocation is None else allocation.pod_id,
            "name": None if allocation is None else allocation.name,
            "phase": ("pending" if allocation is None else allocation.phase) if pod is None else pod.status.lower(),
        }

    def _terminate(self, job: Job) -> None:
        pod = self._owned(job)
        if pod is not None:
            self.provider.delete(pod.id)
            if self.provider.get(pod.id) is not None:
                raise RuntimeError("Generation Pod termination is not confirmed")
            logger.info("Round {} deleted generation pod {}", job.snapshot.inputs.number, pod.id)
        if job.allocation is not None and job.allocation.phase != "terminated":
            job.allocation.phase = "terminated"
            self.store.save(job)

    async def terminate(self, job: Job) -> None:
        await _threaded(lambda: self._terminate(job))
