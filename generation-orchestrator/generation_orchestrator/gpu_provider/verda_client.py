import asyncio
import time
from types import TracebackType
from typing import Any, Self

import httpx
from loguru import logger
from pydantic import BaseModel

from .common import ContainerInfo, GPUClientError, GPUProvider


class VerdaClientError(GPUClientError):
    """Verda-specific client error."""


class VerdaAuthError(VerdaClientError):
    """Verda authentication error."""


class ContainerDeployConfig(BaseModel):
    """Configuration for Verda container deployment."""

    image: str
    exposed_port: int = 10006
    compute_name: str = "H200"
    compute_size: int = 1
    min_replicas: int = 1
    max_replicas: int = 1
    concurrent_requests_per_replica: int = 8
    is_spot: bool = True
    healthcheck_enabled: bool = True
    healthcheck_path: str = "/health"
    scale_down_delay_seconds: int = 3600
    scale_up_delay_seconds: int = 600
    env: dict[str, str] = {}


class VerdaContainerResponse(BaseModel):
    """Container deployment info returned by Verda API."""

    name: str
    endpoint_base_url: str | None = None
    is_spot: bool = False
    created_at: str | None = None


class VerdaClient:
    """Async Verda client with OAuth 2.0 authentication."""

    BASE_URL = "https://api.verda.com/v1"
    TOKEN_EXPIRY_BUFFER_SECONDS = 60  # Refresh token the 60s before expiry
    ERROR_BODY_MAX_LENGTH = 200

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        timeout: float = 60.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._http_client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        self._http_client = httpx.AsyncClient(timeout=self._timeout)
        await self._ensure_valid_token()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._access_token = None
        self._token_expires_at = 0

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            raise RuntimeError("Client not initialized. Use 'async with'.")
        return self._http_client

    def _now(self) -> float:
        """Return current monotonic time. Override in tests for deterministic behavior."""
        return time.monotonic()

    def _is_token_expired(self) -> bool:
        return self._now() >= (self._token_expires_at - self.TOKEN_EXPIRY_BUFFER_SECONDS)

    async def _fetch_token(self) -> None:
        """Fetch a new OAuth 2.0 access token."""
        try:
            response = await self.http_client.post(
                f"{self.BASE_URL}/oauth2/token",
                json={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            response.raise_for_status()
            data = response.json()

            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = self._now() + expires_in

            logger.debug(f"Obtained Verda access token, expires in {expires_in}s")
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to obtain Verda access token: {e.response.text}")
            raise VerdaAuthError(f"Authentication failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"Network error during Verda authentication: {e}")
            raise VerdaAuthError(f"Network error during authentication: {e}") from e

    async def _ensure_valid_token(self) -> None:
        """Ensure we have a valid access token, refreshing if necessary."""
        if self._access_token is None or self._is_token_expired():
            async with self._token_lock:
                # Double-check after acquiring lock to avoid redundant refreshes
                if self._access_token is None or self._is_token_expired():
                    await self._fetch_token()

    def _auth_headers(self) -> dict[str, str]:
        if self._access_token is None:
            raise RuntimeError("No access token available")
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        retry_on_auth_error: bool = True,
    ) -> httpx.Response:
        """Make an authenticated request, refreshing token if needed."""
        await self._ensure_valid_token()

        url = f"{self.BASE_URL}{endpoint}"
        headers = self._auth_headers()

        try:
            response = await self.http_client.request(
                method,
                url,
                headers=headers,
                json=json,
            )

            # Handle token expiration mid-request
            if response.status_code == 401 and retry_on_auth_error:
                logger.debug("Token expired, refreshing and retrying request")
                self._token_expires_at = 0  # Force token refresh
                return await self._request(method, endpoint, json=json, retry_on_auth_error=False)

            response.raise_for_status()
            return response

        except httpx.HTTPStatusError as e:
            body = e.response.text[: self.ERROR_BODY_MAX_LENGTH]
            logger.error(f"Verda API error: {e.response.status_code} - {e.response.text}")
            raise VerdaClientError(f"API error {e.response.status_code}: {body}") from e
        except httpx.RequestError as e:
            logger.error(f"Network error calling Verda API: {e}")
            raise VerdaClientError(f"Network error: {e}") from e

    async def _list_containers_raw(
        self,
        *,
        name: str | None = None,
        prefix: str | None = None,
    ) -> list[VerdaContainerResponse]:
        """List containers (raw API response), optionally filtered by exact name or prefix."""
        response = await self._request("GET", "/container-deployments")
        data = response.json()

        # Handle both list response and wrapped response
        items = data if isinstance(data, list) else data.get("deployments", data.get("items", []))

        containers = [VerdaContainerResponse(**item) for item in items]

        if name:
            containers = [c for c in containers if c.name == name]
        elif prefix:
            containers = [c for c in containers if c.name.startswith(prefix)]

        return containers

    async def get_container(self, name: str) -> ContainerInfo | None:
        """Get container by exact name. Returns None if not found."""
        containers = await self._list_containers_raw(name=name)
        if not containers:
            return None
        c = containers[0]
        if not c.endpoint_base_url:
            return None
        return ContainerInfo(
            name=c.name,
            url=c.endpoint_base_url.rstrip("/"),
            delete_identifier=c.name,
            provider=GPUProvider.VERDA,
        )

    async def deploy_container(
        self,
        name: str,
        *,
        config: ContainerDeployConfig | None = None,
        image: str | None = None,
        resource_name: str | None = None,
        port: int | None = None,
        concurrency: int | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Deploy a new container.

        Either provide a full config, or use individual parameters (which override config).
        """
        # Resolve: explicit param > config > default
        _image = image or (config.image if config else None)
        if not _image:
            raise ValueError("image is required")

        _resource = resource_name or (config.compute_name if config else "H200")
        _port = port or (config.exposed_port if config else 10006)
        _concurrency = concurrency or (config.concurrent_requests_per_replica if config else 8)
        _env = {**(config.env if config else {}), **(env or {})}

        # Other settings from config or defaults
        _healthcheck_enabled = config.healthcheck_enabled if config else True
        _healthcheck_path = config.healthcheck_path if config else "/health"
        _compute_size = config.compute_size if config else 1
        _min_replicas = config.min_replicas if config else 1
        _max_replicas = config.max_replicas if config else 1
        _scale_down_delay = config.scale_down_delay_seconds if config else 3600
        _scale_up_delay = config.scale_up_delay_seconds if config else 600
        _is_spot = config.is_spot if config else True

        # Build container config
        container_config: dict[str, Any] = {
            "image": _image,
            "exposed_port": _port,
            "healthcheck": {
                "enabled": _healthcheck_enabled,
                "port": _port,
                "path": _healthcheck_path,
            },
        }
        if _env:
            container_config["env"] = [
                {"name": k, "value_or_reference_to_secret": v, "type": "plain"} for k, v in _env.items()
            ]

        payload = {
            "name": name,
            "container_registry_settings": {"is_private": False},
            "containers": [container_config],
            "compute": {
                "name": _resource,
                "size": _compute_size,
            },
            "scaling": {
                "min_replica_count": _min_replicas,
                "max_replica_count": _max_replicas,
                "scale_down_policy": {"delay_seconds": _scale_down_delay},
                "scale_up_policy": {"delay_seconds": _scale_up_delay},
                "queue_message_ttl_seconds": 600,
                "concurrent_requests_per_replica": _concurrency,
                "scaling_triggers": {
                    "queue_load": {"threshold": _concurrency},
                    "cpu_utilization": {"enabled": False, "threshold": 80},
                    "gpu_utilization": {"enabled": False, "threshold": 80},
                },
            },
            "is_spot": _is_spot,
        }

        logger.debug(f"Deploying container {name}")
        response = await self._request("POST", "/container-deployments", json=payload)
        data = response.json()

        logger.info(f"Deployed container {name}: {data.get('endpoint_base_url', 'N/A')}")

    async def delete_container(self, name: str, raise_on_failure: bool = False) -> bool:
        """Delete the container by name. Returns True if deleted or already gone."""
        try:
            logger.debug(f"Deleting container {name}")
            await self._request("DELETE", f"/container-deployments/{name}")
            logger.info(f"Deleted container: {name}")
            return True
        except VerdaClientError as e:
            # Treat 404 as a success (idempotent delete)
            if "404" in str(e):
                logger.debug(f"Container {name} already deleted or not found")
                return True
            logger.error(f"Failed to delete container {name}: {e}")
            if raise_on_failure:
                raise
            return False

    async def delete_containers_by_name(self, name: str) -> int:
        """Delete all containers with the exact name. Returns count deleted."""
        containers = await self._list_containers_raw(name=name)
        if not containers:
            return 0

        results = await asyncio.gather(
            *[self.delete_container(c.name) for c in containers],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def delete_containers_by_prefix(self, prefix: str) -> int:
        """
        Delete all containers matching prefix. Returns count deleted.

        Example: delete_containers_by_prefix("miner-5") deletes
        miner-5-abc123, miner-5-def456, etc.
        """
        containers = await self._list_containers_raw(prefix=prefix)
        if not containers:
            return 0

        results = await asyncio.gather(
            *[self.delete_container(c.name) for c in containers],
            return_exceptions=True,
        )
        deleted = sum(1 for r in results if r is True)

        if deleted:
            logger.info(f"Deleted {deleted} containers matching prefix '{prefix}'")
        return deleted
