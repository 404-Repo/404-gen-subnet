import asyncio
from enum import Enum
from types import TracebackType
from typing import Self

import httpx
from loguru import logger
from pydantic import BaseModel
from subnet_common.graceful_shutdown import GracefulShutdown
from targon.client.serverless import ServerlessResourceListItem

from generation_orchestrator.settings import Settings

from .targon_client import (
    ContainerDeployConfig as TargonContainerDeployConfig,
    TargonClient,
    TargonClientError,
)
from .verda_client import (
    ContainerDeployConfig as VerdaContainerDeployConfig,
    ContainerInfo as VerdaContainerInfo,
    VerdaClient,
    VerdaClientError,
)


class GPUProviderError(Exception):
    """Base exception for all GPU provider errors."""

    pass


class TargonError(GPUProviderError):
    """Targon-specific error."""

    pass


class VerdaError(GPUProviderError):
    """Verda-specific error."""

    pass


class GPUProvider(str, Enum):
    TARGON = "targon"
    VERDA = "verda"


# (gpu_type, provider) -> resource_name
RESOURCE_NAMES: dict[tuple[str, GPUProvider], str] = {
    ("H200", GPUProvider.TARGON): "h200-small",
    ("H200", GPUProvider.VERDA): "H200",
}


class ContainerInfo(BaseModel):
    """Container information from any GPU provider."""

    name: str
    url: str
    delete_identifier: str
    provider: GPUProvider

    @classmethod
    def from_targon(cls, container: ServerlessResourceListItem) -> "ContainerInfo | None":
        if not container.url:
            return None
        return cls(
            name=container.name,
            url=container.url,
            delete_identifier=container.uid,
            provider=GPUProvider.TARGON,
        )

    @classmethod
    def from_verda(cls, container: VerdaContainerInfo) -> "ContainerInfo | None":
        if not container.endpoint_base_url:
            return None
        return cls(
            name=container.name,
            url=container.endpoint_base_url,
            delete_identifier=container.name,
            provider=GPUProvider.VERDA,
        )


class DeployedContainer(BaseModel):
    """Result of a successful container deployment."""

    info: ContainerInfo
    generation_token: str | None = None


class TargonClientAdapter:
    """Adapter to make TargonClient conform to a common interface."""

    def __init__(self, client: TargonClient) -> None:
        self._client = client

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def get_container(self, name: str) -> ContainerInfo | None:
        try:
            container = await self._client.get_container(name)
            return ContainerInfo.from_targon(container) if container else None
        except TargonClientError as e:
            raise TargonError(str(e)) from e

    async def deploy_container(self, name: str, config: TargonContainerDeployConfig) -> None:
        try:
            await self._client.deploy_container(name, config)
        except TargonClientError as e:
            raise TargonError(str(e)) from e

    async def delete_container(self, identifier: str) -> None:
        try:
            await self._client.delete_container(identifier)
        except TargonClientError as e:
            raise TargonError(str(e)) from e

    async def delete_containers_by_name(self, name: str) -> int:
        try:
            return await self._client.delete_containers_by_name(name)
        except TargonClientError as e:
            raise TargonError(str(e)) from e

    async def delete_containers_by_prefix(self, prefix: str) -> int:
        try:
            return await self._client.delete_containers_by_prefix(prefix)
        except TargonClientError as e:
            raise TargonError(str(e)) from e


class VerdaClientAdapter:
    """Adapter to make VerdaClient conform to the common interface."""

    def __init__(self, client: VerdaClient) -> None:
        self._client = client

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def get_container(self, name: str) -> ContainerInfo | None:
        try:
            container = await self._client.get_container(name)
            return ContainerInfo.from_verda(container) if container else None
        except VerdaClientError as e:
            raise VerdaError(str(e)) from e

    async def deploy_container(self, name: str, config: VerdaContainerDeployConfig) -> None:
        try:
            await self._client.deploy_container(name, config)
        except VerdaClientError as e:
            raise VerdaError(str(e)) from e

    async def delete_container(self, identifier: str) -> None:
        try:
            await self._client.delete_container(identifier)
        except VerdaClientError as e:
            raise VerdaError(str(e)) from e

    async def delete_containers_by_name(self, name: str) -> int:
        try:
            return await self._client.delete_containers_by_name(name)
        except VerdaClientError as e:
            raise VerdaError(str(e)) from e

    async def delete_containers_by_prefix(self, prefix: str) -> int:
        try:
            return await self._client.delete_containers_by_prefix(prefix)
        except VerdaClientError as e:
            raise VerdaError(str(e)) from e


ProviderClient = TargonClientAdapter | VerdaClientAdapter


class GPUProviderManager:
    """Manages GPU provider selection and container lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._providers = self._get_enabled_providers()

    def _get_enabled_providers(self) -> list[GPUProvider]:
        providers = [GPUProvider.TARGON]
        if self._settings.verda_enabled:
            providers.append(GPUProvider.VERDA)
        return providers

    @property
    def provider_count(self) -> int:
        return len(self._providers)

    def get_provider(self, attempt: int) -> GPUProvider:
        """Get a provider for a given attempt number (round-robin)."""
        return self._providers[attempt % len(self._providers)]

    def get_generation_token(self, provider: GPUProvider) -> str | None:
        """Get a generation token for a provider if needed (e.g., Verda)."""
        if provider == GPUProvider.VERDA and self._settings.verda_generation_token:
            return self._settings.verda_generation_token.get_secret_value()
        return None

    def create_client(self, provider: GPUProvider) -> ProviderClient:
        """Create a client adapter for the given provider."""
        if provider == GPUProvider.TARGON:
            return TargonClientAdapter(TargonClient(api_key=self._settings.targon_api_key.get_secret_value()))
        elif provider == GPUProvider.VERDA:
            if not self._settings.verda_client_id or not self._settings.verda_client_secret:
                raise GPUProviderError("Verda credentials not configured")
            return VerdaClientAdapter(
                VerdaClient(
                    client_id=self._settings.verda_client_id.get_secret_value(),
                    client_secret=self._settings.verda_client_secret.get_secret_value(),
                )
            )
        raise ValueError(f"Unknown provider: {provider}")

    def create_deploy_config(
        self,
        provider: GPUProvider,
        image: str,
        gpu_type: str,
    ) -> TargonContainerDeployConfig | VerdaContainerDeployConfig:
        """Create provider-specific deploy configuration."""
        resource_name = RESOURCE_NAMES.get((gpu_type, provider))
        if not resource_name:
            raise ValueError(f"Unknown GPU type {gpu_type} for provider {provider}")

        if provider == GPUProvider.TARGON:
            return TargonContainerDeployConfig(
                image=image,
                resource_name=resource_name,
                port=self._settings.generation_port,
                container_concurrency=self._settings.max_concurrent_prompts_per_miner + 1,
            )
        else:
            return VerdaContainerDeployConfig(
                image=image,
                exposed_port=self._settings.generation_port,
                compute_name=resource_name,
                concurrent_requests_per_replica=self._settings.max_concurrent_prompts_per_miner + 1,
            )

    async def try_get_pod(
        self,
        provider: GPUProvider,
        name: str,
        image: str,
        gpu_type: str,
        shutdown: GracefulShutdown,
        *,
        timeout: float = 600.0,  # noqa: ASYNC109
        check_interval: float = 5.0,
        cleanup_existing: bool = True,
    ) -> ContainerInfo | None:
        """
        Try to deploy a container and wait for it to become visible.

        This handles a single attempt on a single provider.
        Returns ContainerInfo if successful, None if failed or timed out.
        Raises GPUProviderError on API errors.
        """
        config = self.create_deploy_config(provider, image, gpu_type)

        async with self.create_client(provider) as client:
            if cleanup_existing:
                deleted = await client.delete_containers_by_name(name)
                if deleted:
                    logger.debug(f"Cleaned up {deleted} existing container(s) named {name}")

            logger.info(f"Deploying {name} on {provider.value}")

            # Type narrowing for mypy
            if isinstance(client, TargonClientAdapter) and isinstance(config, TargonContainerDeployConfig):
                await client.deploy_container(name, config)
            elif isinstance(client, VerdaClientAdapter) and isinstance(config, VerdaContainerDeployConfig):
                await client.deploy_container(name, config)
            else:
                raise GPUProviderError(f"Mismatched client/config for provider {provider}")

            # Wait for the container to become visible with URL
            deadline = asyncio.get_running_loop().time() + timeout
            while not shutdown.should_stop:
                container = await client.get_container(name)
                if container:
                    logger.info(f"Container {name} visible on {provider.value}: {container.url}")
                    return container

                if asyncio.get_running_loop().time() >= deadline:
                    logger.warning(f"Container {name} not visible within {timeout}s on {provider.value}")
                    return None

                await shutdown.wait(timeout=check_interval)

            return None

    async def delete_container(self, container: ContainerInfo) -> None:
        """Delete a container using the appropriate provider client."""
        async with self.create_client(container.provider) as client:
            await client.delete_container(container.delete_identifier)
            logger.info(f"Deleted container {container.name} on {container.provider.value}")

    async def get_healthy_pod(
        self,
        name: str,
        image: str,
        gpu_type: str,
        shutdown: GracefulShutdown,
        *,
        deploy_timeout: float | None = None,
        warmup_timeout: float | None = None,
        check_interval: float | None = None,
    ) -> DeployedContainer | None:
        """
        Get a healthy container, trying multiple providers if needed.

        Uses settings for retry parameters:
        - pod_acquisition_max_attempts: max attempts (default 720 = 24h)
        - pod_acquisition_retry_interval_seconds: delay between attempts (default 2 min)

        Retry logic:
        - Retries on POD acquisition failures (deploy failed, not visible, no GPU)
        - Does NOT retry on warmup failure (that's a miner issue)

        Returns DeployedContainer if successful, None if:
        - All POD attempts failed, OR
        - POD was acquired but warmup failed
        """
        max_attempts = self._settings.pod_acquisition_max_attempts
        retry_interval = self._settings.pod_acquisition_retry_interval_seconds
        deploy_timeout = deploy_timeout or self._settings.pod_visibility_timeout_seconds
        warmup_timeout = warmup_timeout or self._settings.pod_warmup_timeout_seconds
        check_interval = check_interval or self._settings.check_pod_interval_seconds

        for attempt in range(max_attempts):
            provider = self.get_provider(attempt)
            logger.info(f"POD attempt {attempt + 1}/{max_attempts} on {provider.value}")

            try:
                container = await self.try_get_pod(
                    provider=provider,
                    name=name,
                    image=image,
                    gpu_type=gpu_type,
                    shutdown=shutdown,
                    timeout=deploy_timeout,
                    check_interval=check_interval,
                )
            except GPUProviderError as e:
                logger.warning(f"Failed to deploy on {provider.value}: {e}")
                container = None

            if not container:
                logger.info(f"Waiting {retry_interval}s before next POD attempt")
                await shutdown.wait(timeout=retry_interval)
                if shutdown.should_stop:
                    return None
                continue  # Retry with the next provider

            # POD acquired, now check health (NO retry on failure)
            logger.info(f"Waiting for {name} to become healthy (timeout: {warmup_timeout}s)")
            if await wait_for_healthy(
                container.url,
                shutdown,
                timeout=warmup_timeout,
                check_interval=check_interval,
            ):
                logger.info(f"Container {name} healthy on {provider.value}")
                return DeployedContainer(
                    info=container,
                    generation_token=self.get_generation_token(provider),
                )

            logger.warning(f"Container {name} failed warmup on {provider.value}")
            return None

        logger.error(f"Failed to get POD after {max_attempts} attempts")
        return None

    async def cleanup_by_prefix(self, prefix: str) -> int:
        """Delete all containers matching the prefix across ALL enabled providers."""
        total_deleted = 0
        for provider in self._providers:
            try:
                async with self.create_client(provider) as client:
                    deleted = await client.delete_containers_by_prefix(prefix)
                    total_deleted += deleted
                    if deleted:
                        logger.info(f"Cleaned up {deleted} containers on {provider.value} matching '{prefix}'")
            except GPUProviderError as e:
                logger.warning(f"Failed to cleanup on {provider.value}: {e}")
        return total_deleted


async def wait_for_healthy(
    url: str,
    shutdown: GracefulShutdown,
    *,
    timeout: float = 300.0,  # noqa: ASYNC109
    check_interval: float = 5.0,
) -> bool:
    """Wait for the container health endpoint to return 200."""
    health_url = f"{url}/health"
    deadline = asyncio.get_running_loop().time() + timeout

    async with httpx.AsyncClient(timeout=30.0) as http:
        while not shutdown.should_stop:
            try:
                response = await http.get(health_url)
                if response.status_code == 200:
                    return True
            except httpx.RequestError as e:
                logger.debug(f"Health check failed for {url}: {e}")

            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(f"Container at {url} not healthy within {timeout}s")
                return False

            await shutdown.wait(timeout=check_interval)

    return False
