"""RunPod v2 adapter. No implicit retries, especially for POST /pods."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

from .models import MARKERS, Pod, object_dict, string

API = "https://api.runpod.io/v2"


class ProviderUnavailable(RuntimeError):
    def __init__(
        self, message: str, *, status: int | None = None, retry_after: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class CreateUnknown(ProviderUnavailable):
    """The create may have reached RunPod; reconcile instead of POSTing again."""


class CreateRejected(ProviderUnavailable):
    """RunPod explicitly rejected the request without creating a Pod."""


class PodProvider(Protocol):
    def list(self) -> list[Pod]: ...
    def get(self, pod_id: str) -> Pod | None: ...
    def create(self, body: dict[str, object]) -> Pod: ...
    def delete(self, pod_id: str) -> None: ...


class RunPodClient:
    def __init__(self, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key
        self._client = client

    @staticmethod
    def http_client() -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(30, connect=5),
            transport=httpx.HTTPTransport(retries=0),
            follow_redirects=False,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                API + path,
                json=body,
                params=params,
                headers={"Authorization": f"Bearer {self._api_key}"},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ProviderUnavailable("RunPod request did not complete") from None

    @staticmethod
    def _error(response: httpx.Response) -> ProviderUnavailable:
        # Provider error bodies may echo environment variables or credentials.
        return ProviderUnavailable(
            "RunPod request failed",
            status=response.status_code,
            retry_after=response.headers.get("Retry-After"),
        )

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, object]:
        try:
            return object_dict(response.json())
        except ValueError:
            raise ProviderUnavailable("Malformed RunPod response") from None

    @staticmethod
    def _pod(data: object) -> Pod:
        try:
            value = object_dict(data)
            environment = object_dict({} if value.get("env") is None else value["env"])
            # Discard all secrets before returning provider data to the core.
            markers = {key: string(environment[key]) for key in MARKERS if key in environment}
            return Pod(
                string(value["id"]),
                string(value["name"]),
                string(value["image"]),
                string(value["status"]),
                markers,
            )
        except (KeyError, ValueError):
            raise ProviderUnavailable("Malformed RunPod Pod") from None

    def list(self) -> list[Pod]:
        pods: dict[str, Pod] = {}
        cursor: str | None = None
        cursors: set[str] = set()
        while True:
            params = {"limit": "1000", "includeClusterPods": "false"}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._request("GET", "/pods", params=params)
            if response.status_code != 200:
                raise self._error(response)
            value = self._object(response)
            try:
                page = value["pods"]
                pagination = object_dict(value["pagination"])
                if not isinstance(page, list) or type(pagination["hasNextPage"]) is not bool:
                    raise ValueError("Invalid page")
                for item in page:
                    pod = self._pod(item)
                    if pod.id in pods and pods[pod.id] != pod:
                        raise ValueError("Conflicting inventory")
                    pods[pod.id] = pod
                if not pagination["hasNextPage"]:
                    return list(pods.values())
                cursor = string(pagination["nextCursor"])
                if cursor in cursors:
                    raise ValueError("Repeated cursor")
                cursors.add(cursor)
            except (KeyError, ValueError):
                raise ProviderUnavailable("Incomplete RunPod inventory") from None

    def get(self, pod_id: str) -> Pod | None:
        response = self._request("GET", f"/pods/{quote(pod_id, safe='')}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._error(response)
        pod = self._pod(self._object(response))
        if pod.id != pod_id:
            raise ProviderUnavailable("RunPod returned a different Pod ID")
        return pod

    def create(self, body: dict[str, object]) -> Pod:
        try:
            response = self._request("POST", "/pods", body=body)
        except ProviderUnavailable:
            raise CreateUnknown("RunPod creation is unconfirmed") from None
        if response.status_code in (400, 401, 403, 404, 422, 429):
            raise CreateRejected(
                "RunPod rejected Pod creation",
                status=response.status_code,
                retry_after=response.headers.get("Retry-After"),
            )
        if response.status_code not in (200, 201):
            raise CreateUnknown(
                "RunPod creation is unconfirmed",
                status=response.status_code,
                retry_after=response.headers.get("Retry-After"),
            )
        try:
            return self._pod(self._object(response))
        except ProviderUnavailable:
            raise CreateUnknown("RunPod creation reply is incomplete") from None

    def delete(self, pod_id: str) -> None:
        response = self._request("DELETE", f"/pods/{quote(pod_id, safe='')}")
        if response.status_code not in (204, 404):
            raise self._error(response)
        # This acknowledgement alone does not establish absence. The core GETs.
