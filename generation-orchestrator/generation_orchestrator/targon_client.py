from types import TracebackType
from typing import Self

from loguru import logger
from pydantic import BaseModel, Field
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


class TargonClientError(Exception):
    pass


class ContainerDeployConfig(BaseModel):
    """Configuration for Targon container deployment."""

    image: str
    container_concurrency: int
    command: list[str] = Field(default_factory=lambda: ["sh", "-c"])
    args: list[str] = Field(default_factory=lambda: ["python serve.py"])
    resource_name: str = "h200-small"
    port: int = 10006


class TargonClient:
    """Async Targon client."""

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
            logger.error(f"Failed to list containers: {e}")
            raise TargonClientError(f"Failed to list containers: {e}") from e

    async def get_container(self, name: str) -> ServerlessResourceListItem | None:
        """Get container by exact name. Returns None if not found."""
        containers = await self.list_containers(name=name)
        return containers[0] if containers else None

    async def deploy_container(self, name: str, config: ContainerDeployConfig) -> None:
        """Deploy a new container. Does not wait for it to be visible."""
        request = CreateServerlessResourceRequest(
            name=name,
            container=TargonContainerConfig(
                image=config.image,
                command=config.command,
                args=config.args,
            ),
            resource_name=config.resource_name,
            network=NetworkConfig(
                port=PortConfig(port=config.port),
                visibility="external",
            ),
            scaling=AutoScalingConfig(
                min_replicas=1,
                max_replicas=1,
                container_concurrency=config.container_concurrency,
                target_concurrency=config.container_concurrency,
            ),
        )
        try:
            logger.debug(f"Requested container deploy {name}")
            response = await self.client.async_serverless.deploy_container(request)
            logger.info(f"Deployed container {name} ({response.uid})")
        except (TargonError, APIError) as e:
            logger.error(f"Failed to deploy container {name}: {e}")
            raise TargonClientError(f"Failed to deploy container: {e}") from e

    async def delete_container(self, uid: str, raise_on_failure: bool = False) -> None:
        """Delete container by UID."""
        try:
            logger.debug(f"Requested container delete {uid}")
            await self.client.async_serverless.delete_container(uid)
            logger.info(f"Deleted container: {uid}")
        except (TargonError, APIError) as e:
            logger.error(f"Failed to delete container: {e}")
            if raise_on_failure:
                raise TargonClientError(f"Failed to delete container: {e}") from e

    async def delete_containers_by_name(self, name: str) -> int:
        """Delete all containers with the exact name. Returns count deleted."""
        containers = await self.list_containers(name=name)
        for c in containers:
            await self.delete_container(c.uid)
        return len(containers)

    async def delete_containers_by_prefix(self, prefix: str) -> int:
        """
        Delete all containers matching prefix. Returns count deleted.

        Example: delete_containers_by_prefix("miner-5") deletes
        miner-5-5e7eserr2a, miner-5-abc1234567, etc.
        """
        containers = await self.list_containers(prefix=prefix)
        for c in containers:
            await self.delete_container(c.uid)
        if containers:
            logger.info(f"Deleted {len(containers)} containers matching prefix '{prefix}'")
        return len(containers)
