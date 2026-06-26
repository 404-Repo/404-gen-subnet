import asyncio
from types import TracebackType
from typing import Any, Self

import httpx
from loguru import logger
from pydantic import BaseModel

from .common import ContainerInfo, GPUClientError, GPUProvider


class RunpodClientError(GPUClientError):
    """Runpod-specific client error."""


# Runpod identifies GPUs via `gpuTypeIds` — string IDs specific to their catalog.
# Values below reflect current Runpod naming; update if the catalog changes.
_RUNPOD_GPU_TYPE_IDS: dict[str, str] = {
    "H200": "NVIDIA H200",  # SXM (api_specification.md requires SXM); NVL is "NVIDIA H200 NVL"
    "H100": "NVIDIA H100 80GB HBM3",  # SXM variant; PCIe/NVL are separate SKUs
    "B200": "NVIDIA B200",
    "RTX6000Pro": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
}


def _resolve_gpu_type_id(gpu_type: str) -> str:
    resolved = _RUNPOD_GPU_TYPE_IDS.get(gpu_type)
    if resolved is None:
        known = ", ".join(sorted(_RUNPOD_GPU_TYPE_IDS))
        raise RunpodClientError(f"Runpod has no gpuTypeId for {gpu_type}. Known: {known}")
    return resolved


class RunpodPodResponse(BaseModel):
    """Subset of Runpod's pod response that we care about."""

    id: str
    name: str
    desiredStatus: str | None = None
    runtime: dict[str, Any] | None = None


class RunpodClient:
    """Async Runpod client for the Pods REST API.

    Uses Runpod's proxy URL scheme (`https://{pod_id}-{port}.proxy.runpod.net`) to build
    the container URL — Runpod's API doesn't return a ready-made endpoint the way Verda
    does.
    """

    BASE_URL = "https://rest.runpod.io/v1"
    ERROR_BODY_MAX_LENGTH = 200

    def __init__(
        self,
        api_key: str,
        timeout: float = 60.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._http_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._http_client = httpx.AsyncClient(timeout=self._timeout)
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

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            raise RuntimeError("Client not initialized. Use 'async with'.")
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = await self.http_client.request(
                method,
                url,
                headers=self._auth_headers(),
                json=json,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            body = e.response.text[: self.ERROR_BODY_MAX_LENGTH]
            logger.error(
                f"Runpod API error: {e.response.status_code} "
                f"url={e.request.url} body={e.response.text!r} "
                f"www-authenticate={e.response.headers.get('www-authenticate', '<none>')}"
            )
            raise RunpodClientError(f"API error {e.response.status_code}: {body}") from e
        except httpx.RequestError as e:
            logger.error(f"Network error calling Runpod API: {e}")
            raise RunpodClientError(f"Network error: {e}") from e

    def _proxy_url(self, pod_id: str, port: int) -> str:
        """Runpod's built-in HTTP proxy scheme."""
        return f"https://{pod_id}-{port}.proxy.runpod.net"

    def _to_container_info(self, pod: RunpodPodResponse, port: int) -> ContainerInfo | None:
        """Build ContainerInfo from a Runpod pod response. Returns None if not ready."""
        if not pod.id:
            return None
        return ContainerInfo(
            name=pod.name,
            url=self._proxy_url(pod.id, port),
            delete_identifier=pod.id,
            provider=GPUProvider.RUNPOD,
        )

    async def _list_pods_raw(
        self,
        *,
        name: str | None = None,
        prefix: str | None = None,
    ) -> list[RunpodPodResponse]:
        response = await self._request("GET", "/pods")
        data = response.json()
        items = data if isinstance(data, list) else data.get("pods", data.get("items", []))
        pods = [RunpodPodResponse(**item) for item in items]

        if name:
            pods = [p for p in pods if p.name == name]
        elif prefix:
            pods = [p for p in pods if p.name.startswith(prefix)]
        return pods

    async def get_container(self, name: str, port: int = 10006) -> ContainerInfo | None:
        """Get a pod by name. Returns None if not found or not yet addressable."""
        pods = await self._list_pods_raw(name=name)
        if not pods:
            return None
        return self._to_container_info(pods[0], port=port)

    async def deploy_container(
        self,
        name: str,
        *,
        image: str,
        gpu_type: str = "H200",
        gpu_count: int = 4,
        port: int = 10006,
        concurrency: int = 8,  # noqa: ARG002 — Runpod has no concurrency knob, kept for API parity
        env: dict[str, str] | None = None,
    ) -> None:
        """Create a Runpod pod. Does not wait for it to be visible."""
        gpu_type_id = _resolve_gpu_type_id(gpu_type)

        payload: dict[str, Any] = {
            "name": name,
            "imageName": image,
            "gpuTypeIds": [gpu_type_id],
            "gpuCount": gpu_count,
            "containerDiskInGb": 500,
            "volumeInGb": 0,
            "ports": [f"{port}/http"],
            "cloudType": "SECURE",
            "supportPublicIp": True,
        }
        if env:
            payload["env"] = env

        logger.debug(f"Deploying pod {name}")
        response = await self._request("POST", "/pods", json=payload)
        data = response.json()
        logger.info(f"Deployed pod {name}: id={data.get('id', 'N/A')}")

    async def delete_container(self, pod_id: str) -> bool:
        """Delete a pod by id. Returns True if deleted or already gone."""
        try:
            logger.debug(f"Deleting pod {pod_id}")
            await self._request("DELETE", f"/pods/{pod_id}")
            logger.info(f"Deleted pod: {pod_id}")
            return True
        except RunpodClientError as e:
            if "404" in str(e):  # idempotent delete — already gone is success
                return True
            logger.error(f"Failed to delete pod {pod_id}: {e}")
            return False

    async def delete_containers_by_name(self, name: str) -> int:
        pods = await self._list_pods_raw(name=name)
        if not pods:
            return 0
        results = await asyncio.gather(
            *[self.delete_container(p.id) for p in pods],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def delete_containers_by_prefix(self, prefix: str) -> int:
        pods = await self._list_pods_raw(prefix=prefix)
        if not pods:
            return 0
        results = await asyncio.gather(
            *[self.delete_container(p.id) for p in pods],
            return_exceptions=True,
        )
        deleted = sum(1 for r in results if r is True)
        if deleted:
            logger.info(f"Deleted {deleted} pods matching prefix '{prefix}'")
        return deleted
