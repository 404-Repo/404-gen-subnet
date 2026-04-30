import asyncio

import httpx
from loguru import logger
from pydantic import BaseModel

from generation_orchestrator.generation_stop import GenerationStop
from generation_orchestrator.pod_client import PodStatus, check_pod_status
from generation_orchestrator.settings import Settings

from .common import ContainerInfo, GPUClientError, GPUProvider
from .runpod_client import RunpodClient
from .targon_client import TargonClient
from .verda_client import VerdaClient


__all__ = [
    "ContainerInfo",
    "DeployedContainer",
    "GPUProvider",
    "GPUProviderError",
    "GPUProviderManager",
    "wait_for_ready",
]


class GPUProviderError(Exception):
    """GPU provider operation failed."""

    def __init__(self, message: str, provider: GPUProvider) -> None:
        super().__init__(f"[{provider.value}] {message}")
        self.provider = provider


class DeployedContainer(BaseModel):
    """Result of a successful container deployment."""

    info: ContainerInfo
    generation_token: str | None = None


class GPUProviderManager:
    """
    Manages GPU provider selection and container lifecycle.

    Public API:
    - get_provider(index) -> GPUProvider
    - get_generation_token(provider) -> str | None
    - get_healthy_pod(...) -> DeployedContainer | None
    - delete_container(container) -> bool
    - cleanup_by_prefix(prefix) -> int
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._providers = self._build_provider_list()

    def _build_provider_list(self) -> list[GPUProvider]:
        """Build a list of enabled providers from settings, preserving order."""
        provider_map = {
            "targon": GPUProvider.TARGON,
            "verda": GPUProvider.VERDA,
            "runpod": GPUProvider.RUNPOD,
        }
        providers = []
        for name in self._settings.gpu_providers.split(","):
            name = name.strip().lower()
            if name in provider_map:
                providers.append(provider_map[name])
        return providers

    @property
    def provider_count(self) -> int:
        """Number of enabled providers."""
        return len(self._providers)

    def get_provider(self, index: int) -> GPUProvider:
        """Get provider by index (wraps around if index >= provider_count)."""
        return self._providers[index % len(self._providers)]

    def get_generation_token(self, provider: GPUProvider) -> str | None:
        """Get an auth token for a provider if needed (e.g., Verda)."""
        if provider == GPUProvider.VERDA and self._settings.verda_generation_token:
            return self._settings.verda_generation_token.get_secret_value()
        return None

    async def get_healthy_pod(
        self,
        name: str,
        image: str,
        gpu_type: str,
        gpu_count: int,
        stop: GenerationStop,
        replacements_remaining: int,
        *,
        provider: GPUProvider | None = None,
        start_index: int = 0,
    ) -> DeployedContainer | None:
        """
        Get a container that has reached `/status=ready`, with retry logic.

        Args:
            name: Container name.
            image: Docker image to deploy.
            gpu_type: GPU model (e.g., "H200"). Provider clients map this to their SKUs.
            gpu_count: Number of GPUs per pod (e.g., 4 for the 4×H200 spec default).
            stop: Graceful shutdown signal.
            replacements_remaining: Forwarded to `/status` polling so the miner sees the
                current replacement budget during warmup.
            provider: If set, use only this provider for all attempts.
                      If None, round-robin across enabled providers.
            start_index: Starting index for round-robin (ignored if provider is set).

        Returns:
            DeployedContainer if `/status=ready`; None on timeout, stop, `/status=replace`,
            or provider failure.
        """
        # Note on retry policy:
        # - We retry only acquisition/visibility failures (provider downs, no GPU available).
        # - We do NOT retry warmup failures: pods run in parallel, and a warmup failure
        #   usually indicates a faulty image, so it's better to fail fast for that pod.
        max_attempts = self._settings.pod_acquisition_max_attempts
        retry_interval = self._settings.pod_acquisition_retry_interval_seconds
        deploy_timeout = self._settings.pod_visibility_timeout_seconds
        warmup_timeout = self._settings.pod_warmup_timeout_seconds
        check_interval = self._settings.check_pod_interval_seconds

        for attempt in range(max_attempts):
            if stop.should_stop:
                return None

            # Provider selection: fixed or round-robin
            current_provider = provider if provider else self.get_provider(start_index + attempt)
            generation_token = self.get_generation_token(current_provider)

            logger.debug(f"POD attempt {attempt + 1}/{max_attempts} on {current_provider.value}")

            container = await self._try_deploy_pod(
                provider=current_provider,
                name=name,
                image=image,
                gpu_type=gpu_type,
                gpu_count=gpu_count,
                stop=stop,
                timeout=deploy_timeout,
                check_interval=check_interval,
            )

            if not container:
                logger.info(f"Waiting {retry_interval}s before next POD attempt")
                await stop.wait(timeout=retry_interval)
                continue

            logger.debug(f"Waiting for {name} to become ready (timeout: {warmup_timeout}s)")
            ready = await wait_for_ready(
                container.url,
                stop,
                auth_token=generation_token,
                replacements_remaining=replacements_remaining,
                timeout=warmup_timeout,
                check_interval=check_interval,
                log_id=name,
            )

            if ready:
                logger.info(f"Container {name} ready on {current_provider.value}")
                return DeployedContainer(
                    info=container,
                    generation_token=generation_token,
                )

            if stop.should_stop:
                logger.debug(f"Container {name} warmup interrupted by stop on {current_provider.value}")  # type: ignore[unreachable]
                await self.delete_container(container)
                return None

            logger.warning(f"Container {name} failed warmup on {current_provider.value}")
            await self.delete_container(container)
            return None

        logger.error(f"Failed to get POD after {max_attempts} attempts")
        return None

    async def delete_container(self, container: ContainerInfo) -> bool:
        """
        Delete a container. Returns True if successful, False otherwise.
        This is a best-effort operation that logs errors but does not raise.
        """
        try:
            async with self._create_client(container.provider) as client:
                await client.delete_container(container.delete_identifier)
                logger.info(f"Deleted container {container.name} on {container.provider.value}")
                return True
        except GPUClientError as e:
            logger.error(f"Failed to delete container {container.name}: [{container.provider.value}] {e}")
            return False

    async def cleanup_by_prefix(self, prefix: str) -> int:
        """Delete all containers matching the prefix across ALL enabled providers."""
        total_deleted = 0
        for prov in self._providers:
            try:
                async with self._create_client(prov) as client:
                    deleted = await client.delete_containers_by_prefix(prefix)
                    total_deleted += deleted
                    if deleted:
                        logger.info(f"Cleaned up {deleted} containers on {prov.value} matching '{prefix}'")
            except GPUClientError as e:
                logger.warning(f"Failed to cleanup on {prov.value}: {e}")
        return total_deleted

    def _create_client(self, provider: GPUProvider) -> TargonClient | VerdaClient | RunpodClient:
        """Create a client for the given provider."""
        if provider == GPUProvider.TARGON:
            # Settings validation enforces credentials when provider is in GPU_PROVIDERS
            if not self._settings.targon_api_key:
                raise GPUProviderError("Targon API key not configured", GPUProvider.TARGON)
            return TargonClient(api_key=self._settings.targon_api_key.get_secret_value())
        elif provider == GPUProvider.RUNPOD:
            if not self._settings.runpod_api_key:
                raise GPUProviderError("Runpod API key not configured", GPUProvider.RUNPOD)
            return RunpodClient(api_key=self._settings.runpod_api_key.get_secret_value())
        elif provider == GPUProvider.VERDA:
            # Settings validation enforces credentials when provider is in GPU_PROVIDERS
            if not self._settings.verda_client_id or not self._settings.verda_client_secret:
                raise GPUProviderError("Verda credentials not configured", GPUProvider.VERDA)
            return VerdaClient(
                client_id=self._settings.verda_client_id.get_secret_value(),
                client_secret=self._settings.verda_client_secret.get_secret_value(),
            )
        raise ValueError(f"Unknown provider: {provider}")

    async def _try_deploy_pod(
        self,
        *,
        provider: GPUProvider,
        name: str,
        image: str,
        gpu_type: str,
        gpu_count: int,
        stop: GenerationStop,
        timeout: float,
        check_interval: float,
    ) -> ContainerInfo | None:
        """
        Try to deploy a container and wait for it to become visible.
        Returns ContainerInfo if successful, None if failed or timed out.
        """
        try:
            async with self._create_client(provider) as client:
                deleted = await client.delete_containers_by_name(name)
                if deleted:
                    logger.debug(f"Cleaned up {deleted} existing container(s) named {name}")

                logger.info(f"Deploying {name} on {provider.value}")
                env = {}
                if self._settings.hf_token:
                    env["HF_TOKEN"] = self._settings.hf_token.get_secret_value()
                await client.deploy_container(
                    name,
                    image=image,
                    gpu_type=gpu_type,
                    gpu_count=gpu_count,
                    port=self._settings.generation_port,
                    concurrency=2,
                    env=env or None,
                )

                # Wait for the container to become visible with URL
                deadline = asyncio.get_running_loop().time() + timeout
                while not stop.should_stop:
                    container = await client.get_container(name)
                    if container:
                        logger.info(f"Container {name} visible on {provider.value}: {container.url}")
                        return container

                    if asyncio.get_running_loop().time() >= deadline:
                        logger.warning(f"Container {name} not visible within {timeout}s on {provider.value}")
                        await client.delete_containers_by_name(name)
                        return None

                    await stop.wait(timeout=check_interval)

                # Stop signal received - cleanup the deployed container
                logger.debug(f"Stop signal received, cleaning up container {name}")
                await client.delete_containers_by_name(name)
                return None
        except GPUClientError as e:
            logger.warning(f"Failed to deploy on {provider.value}: {e}")
            return None


async def wait_for_ready(
    url: str,
    stop: GenerationStop,
    *,
    auth_token: str | None,
    replacements_remaining: int,
    timeout: float,
    check_interval: float,
    log_id: str,
) -> bool:
    """Wait for a pod to reach `/status=ready` within a single deadline.

    Two-phase walk against one budget:
      1. Poll `/health` until 200 (HTTP server up).
      2. Poll `/status` until `ready` (models loaded, batch-capable).

    Returns True iff `/status=ready` was observed before the deadline. Returns False on
    timeout, stop, or `/status=replace` (any non-ready terminal outcome).
    """
    deadline = asyncio.get_running_loop().time() + timeout

    if not await _wait_for_health(
        url=url, deadline=deadline, auth_token=auth_token, stop=stop, check_interval=check_interval, log_id=log_id
    ):
        return False

    return await _wait_for_status_ready(
        url=url,
        deadline=deadline,
        auth_token=auth_token,
        replacements_remaining=replacements_remaining,
        stop=stop,
        check_interval=check_interval,
        log_id=log_id,
    )


async def _wait_for_health(
    url: str,
    deadline: float,
    auth_token: str | None,
    stop: GenerationStop,
    check_interval: float,
    log_id: str,
) -> bool:
    """Poll `/health` until 200 or the shared deadline expires."""
    health_url = f"{url}/health"
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    async with httpx.AsyncClient(timeout=30.0) as http:
        while not stop.should_stop:
            try:
                response = await http.get(health_url, headers=headers)
                if response.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.TimeoutException):
                pass  # Expected during deployment
            except httpx.RequestError as e:
                logger.debug(f"{log_id}: unexpected /health error for {url}: {type(e).__name__}: {e}")

            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(f"{log_id}: container at {url} /health did not return 200 before deadline")
                return False

            await stop.wait(timeout=check_interval)

    return False


async def _wait_for_status_ready(
    url: str,
    deadline: float,
    auth_token: str | None,
    replacements_remaining: int,
    stop: GenerationStop,
    check_interval: float,
    log_id: str,
) -> bool:
    """Poll `/status` until `ready` or the shared deadline expires.

    `/status=replace` during warmup returns False — the pod is unusable and must be torn
    down. The payload is logged for diagnostics but not propagated (consistent with other
    warmup-phase failures).
    """
    while not stop.should_stop:
        response = await check_pod_status(
            endpoint=url,
            auth_token=auth_token,
            replacements_remaining=replacements_remaining,
            log_id=log_id,
        )
        if response is not None:
            if response.status == PodStatus.READY:
                return True
            if response.status == PodStatus.REPLACE:
                logger.warning(
                    f"{log_id}: container at {url} requested replacement during warmup: payload={response.payload}"
                )
                return False

        if asyncio.get_running_loop().time() >= deadline:
            logger.warning(f"{log_id}: container at {url} /status did not reach 'ready' before deadline")
            return False

        await stop.wait(timeout=check_interval)

    return False
