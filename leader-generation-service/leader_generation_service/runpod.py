"""Manage generation CPU pods using the RunPod v2 client."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import decimal
import time
from collections.abc import Callable
from decimal import Decimal
from typing import TypeVar

import httpx
from bittensor_wallet import Wallet
from loguru import logger
from miner_runtime.pod import POD_NAME_PREFIX, Config, pod_body
from pod_gateway.runpod import CreateUnknown, PodProvider
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.witness.models import Pod
from subnet_common.witness.rounds import repository

from .credentials import ACCESS_TOKEN_MARGIN_SECONDS, AccessCredentials
from .settings import Settings


T = TypeVar("T")


async def _threaded(operation: Callable[[], T]) -> T:
    """Finish provider work before propagating caller cancellation."""
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


class RunpodRunner:
    def __init__(
        self,
        settings: Settings,
        provider: PodProvider,
        http: httpx.Client,
        *,
        shutdown: GracefulShutdown,
    ) -> None:
        self.settings, self.provider, self.http = settings, provider, http
        self.shutdown = shutdown
        self._startup_pods: list[Pod] | None = None

    def _cleanup(self) -> None:
        if self.shutdown.should_stop:
            return
        if self._startup_pods is None:
            logger.info("Reading RunPod inventory for startup cleanup")
            self._startup_pods = [pod for pod in self.provider.list(fresh=True) if pod.name.startswith(POD_NAME_PREFIX)]
            logger.info("RunPod startup cleanup found {} generation pods", len(self._startup_pods))
        while self._startup_pods and not self.shutdown.should_stop:
            self._terminate(self._startup_pods[-1])
            self._startup_pods.pop()

    async def cleanup(self) -> None:
        await _threaded(self._cleanup)

    def _price(self, config: Config) -> Decimal:
        settings = self.settings
        check_failed = "Could not verify RunPod CPU capacity, RAM and price"
        available = ("HIGH", "MEDIUM", "LOW")
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
            if cpu["availability"] not in available:
                logger.error("RunPod CPU {} has no capacity for {} vCPUs", config.cpu_id, config.vcpus)
                raise RuntimeError(check_failed)
            if not any(site["availability"] in available for site in sites):
                location = (
                    "any eligible data center" if config.data_center is None else f"data center {config.data_center}"
                )
                logger.error("RunPod CPU {} has no capacity for {} vCPUs in {}", config.cpu_id, config.vcpus, location)
                raise RuntimeError(check_failed)
            hourly = Decimal(str(cpu["price"]["securePerVcpu"])) * config.vcpus
            if not hourly.is_finite() or hourly <= 0:
                raise decimal.InvalidOperation
            available_ram = int(cpu["ramGbPerVcpu"]) * config.vcpus * 1024
            required_ram = max(8192, config.go_memory_mib + 2048)
            if available_ram < required_ram:
                logger.error(
                    "RunPod CPU {} offers {} MiB RAM but the worker needs {} MiB",
                    config.cpu_id,
                    available_ram,
                    required_ram,
                )
                raise RuntimeError(check_failed)
            min_vcpus, max_vcpus = cpu["vcpu"]["min"], cpu["vcpu"]["max"]
            if not min_vcpus <= config.vcpus <= max_vcpus:
                logger.error(
                    "RunPod CPU {} supports {} to {} vCPUs but {} were requested",
                    config.cpu_id,
                    min_vcpus,
                    max_vcpus,
                    config.vcpus,
                )
                raise RuntimeError(check_failed)
            total = hourly + Decimal(config.disk_gb) * Decimal("0.10") / 730
        except httpx.HTTPError as error:
            reason = (
                f"HTTP {error.response.status_code}"
                if isinstance(error, httpx.HTTPStatusError)
                else type(error).__name__
            )
            logger.error("RunPod CPU catalog request failed ({})", reason)
        except StopIteration:
            logger.error("RunPod CPU catalog does not list {} for {} vCPUs", config.cpu_id, config.vcpus)
        except decimal.DecimalException:
            logger.error("RunPod CPU catalog returned an invalid price for {}", config.cpu_id)
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            logger.error("RunPod CPU catalog returned invalid data for {} ({})", config.cpu_id, type(error).__name__)
        else:
            if total > settings.max_hourly_cost:
                logger.error(
                    "RunPod estimated cost of ${}/hour exceeds the ${}/hour cost ceiling",
                    total,
                    settings.max_hourly_cost,
                )
                raise RuntimeError("CPU allocation exceeds the configured hourly cost ceiling")
            return total
        raise RuntimeError(check_failed)

    def _hotkey(self, hotkey: str) -> str:
        settings = self.settings
        wallet = Wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey, path=str(settings.wallet_path))
        if wallet.hotkey_file.is_encrypted() or wallet.hotkey.ss58_address != hotkey:
            raise ValueError("Operator hotkey changed before pod creation")
        path = settings.wallet_path / settings.wallet_name / "hotkeys" / settings.wallet_hotkey
        return base64.b64encode(path.read_bytes()).decode()

    def _start(self, number: int, hotkey: str, credentials: AccessCredentials, min_validity_seconds: int) -> Pod | None:
        if self.shutdown.should_stop:
            return None
        settings = self.settings
        config = Config(
            generation_image=settings.generation_image,
            competition_repository=repository(settings.github_repo),
            competition_branch=settings.github_branch,
            round_number=number,
            hotkey=hotkey,
            account_id=credentials.account_id,
            witness_url=settings.witness_url,
            cpu_id=settings.cpu_id,
            vcpus=settings.vcpus,
            disk_gb=settings.disk_gb,
            data_center=settings.data_center,
            workers=settings.workers,
            parallel_calls=settings.parallel_calls,
            parallel_preparations=settings.parallel_preparations,
            prover_contexts=settings.prover_contexts,
            go_cpus=settings.go_cpus,
            go_memory_mib=settings.go_memory_mib,
        )
        self._price(config)
        serialized_hotkey = self._hotkey(hotkey)
        if credentials.expires_at < time.time() + min_validity_seconds:
            raise RuntimeError("Access token has insufficient validity for the generation window")
        body = pod_body(config, hotkey=serialized_hotkey, access_token=credentials.access_token)
        if self.shutdown.should_stop:
            return None
        name = body["name"]
        logger.info("Round {} requesting generation pod {}", number, name)
        try:
            pod = self.provider.create(body)
            if pod.name != name:
                raise CreateUnknown("RunPod create did not confirm the requested generation pod")
        except CreateUnknown:
            logger.warning("Round {} creation unconfirmed for generation pod {}", number, name)
            raise
        logger.info("Round {} created generation pod {}", number, pod.id)
        return pod

    async def start(
        self,
        number: int,
        hotkey: str,
        credentials: AccessCredentials,
        *,
        min_validity_seconds: int = ACCESS_TOKEN_MARGIN_SECONDS,
    ) -> Pod | None:
        return await _threaded(lambda: self._start(number, hotkey, credentials, min_validity_seconds))

    def _terminate(self, pod: Pod) -> None:
        logger.info("Deleting generation pod {} ({})", pod.id, pod.name)
        self.provider.delete(pod.id)
        if self.provider.get(pod.id) is not None:
            logger.warning("Generation pod {} is still present after deletion", pod.id)
            raise RuntimeError("Generation Pod termination is not confirmed")
        logger.info("Deleted generation pod {} ({})", pod.id, pod.name)

    async def terminate(self, pod: Pod) -> None:
        await _threaded(lambda: self._terminate(pod))
