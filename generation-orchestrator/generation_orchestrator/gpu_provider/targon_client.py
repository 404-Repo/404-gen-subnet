import asyncio
from types import TracebackType
from typing import Self

from loguru import logger
from targon.client.client import Client
from targon.client.serverless import (
    AutoScalingConfig,
    ContainerConfig as TargonContainerConfig,
    CreateServerlessResourceRequest,
    NetworkConfig,
    PortConfig,
    ServerlessResourceListItem,
)
from targon.core.exceptions import APIError, TargonError

from .common import ContainerInfo, GPUClientError, GPUProvider


class TargonClientError(GPUClientError):
    """Targon-specific client error."""


# Targon bundles (GPU type, count) into single SKU strings. Internal to this client —
# callers pass `(gpu_type, gpu_count)` and we resolve.
_TARGON_SKUS: dict[tuple[str, int], str] = {
    ("H200", 1): "h200-small",
    ("H200", 4): "h200-large",
}


def _resolve_sku(gpu_type: str, gpu_count: int) -> str:
    sku = _TARGON_SKUS.get((gpu_type, gpu_count))
    if sku is None:
        known = ", ".join(sorted(f"{count}x{gtype}" for (gtype, count) in _TARGON_SKUS))
        raise TargonClientError(f"Targon has no SKU for {gpu_count}x{gpu_type}. Known: {known}")
    return sku


class TargonClient:
    """Async Targon client."""

    ERROR_BODY_MAX_LENGTH = 200

    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._client: Client | None = None

    async def __aenter__(self) -> Self:
        self._client = Client(api_key=self._api_key)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with'.")
        return self._client

    async def list_containers(
        self,
        *,
        name: str | None = None,
        prefix: str | None = None,
    ) -> list[ServerlessResourceListItem]:
        """List containers, optionally filtered by the exact name or prefix."""
        try:
            containers: list[ServerlessResourceListItem] = await self.client.async_serverless.list_container()
            if name:
                containers = [c for c in containers if c.name == name]
            elif prefix:
                containers = [c for c in containers if c.name.startswith(prefix)]
            return containers
        except (TargonError, APIError) as e:
            error_msg = str(e)[: self.ERROR_BODY_MAX_LENGTH]
            logger.error(f"Failed to list containers: {e}")
            raise TargonClientError(f"Failed to list containers: {error_msg}") from e

    async def get_container(self, name: str) -> ContainerInfo | None:
        """Get container by exact name. Returns None if not found."""
        containers = await self.list_containers(name=name)
        if not containers:
            return None
        c = containers[0]
        if not c.url:
            return None
        return ContainerInfo(
            name=c.name,
            url=c.url.rstrip("/"),
            delete_identifier=c.uid,
            provider=GPUProvider.TARGON,
        )

    async def deploy_container(
        self,
        name: str,
        *,
        image: str,
        gpu_type: str = "H200",
        gpu_count: int = 4,
        port: int = 10006,
        concurrency: int = 8,
        env: dict[str, str] | None = None,
    ) -> None:
        """Deploy a new container. Does not wait for it to be visible."""
        resource = _resolve_sku(gpu_type, gpu_count)

        request = CreateServerlessResourceRequest(
            name=name,
            container=TargonContainerConfig(image=image, env=env or None),
            resource_name=resource,
            network=NetworkConfig(
                port=PortConfig(port=port),
                visibility="external",
            ),
            scaling=AutoScalingConfig(
                min_replicas=1,
                max_replicas=1,
                container_concurrency=concurrency,
                target_concurrency=concurrency,
            ),
        )
        try:
            logger.debug(f"Deploying container {name}")
            response = await self.client.async_serverless.deploy_container(request)
            logger.info(f"Deployed container {name} ({response.uid})")
        except (TargonError, APIError) as e:
            error_msg = str(e)[: self.ERROR_BODY_MAX_LENGTH]
            logger.error(f"Failed to deploy container {name}: {e}")
            raise TargonClientError(f"Failed to deploy container {name}: {error_msg}") from e

    async def delete_container(self, uid: str) -> bool:
        """Delete container by UID. Returns True if deleted or already gone."""
        try:
            logger.debug(f"Deleting container {uid}")
            await self.client.async_serverless.delete_container(uid)
            logger.info(f"Deleted container: {uid}")
            return True
        except (TargonError, APIError) as e:
            status_code = getattr(e, "status_code", None)
            if status_code is None and hasattr(e, "response"):
                status_code = getattr(e.response, "status_code", None)
            logger.error(f"Failed to delete container {uid}: status={status_code or 'unknown'}")
            return False

    async def delete_containers_by_name(self, name: str) -> int:
        """Delete all containers with exact name. Returns count deleted."""
        containers = await self.list_containers(name=name)
        if not containers:
            return 0

        results = await asyncio.gather(
            *[self.delete_container(c.uid) for c in containers],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def delete_containers_by_prefix(self, prefix: str) -> int:
        """
        Delete all containers matching prefix. Returns count deleted.

        Example: delete_containers_by_prefix("miner-5") deletes
        miner-5-5e7eserr2a, miner-5-abc1234567, etc.
        """
        containers = await self.list_containers(prefix=prefix)
        if not containers:
            return 0

        results = await asyncio.gather(
            *[self.delete_container(c.uid) for c in containers],
            return_exceptions=True,
        )
        deleted = sum(1 for r in results if r is True)

        if deleted:
            logger.info(f"Deleted {deleted} containers matching prefix '{prefix}'")
        return deleted
