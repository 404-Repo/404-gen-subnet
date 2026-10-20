"""RunPod v2 adapter. No implicit retries, especially for POST /pods."""

from __future__ import annotations

import ipaddress
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import quote

import httpx

from .models import MARKERS, Pod, object_dict, string

API = "https://api.runpod.io/v2"
PodList = list[Pod]


class ProviderUnavailable(RuntimeError):
    def __init__(
        self, message: str, *, status: int | None = None, retry_after: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class CreateUnknown(ProviderUnavailable):
    """The create may have reached RunPod; this request is never auto-replayed."""


class CreateRejected(ProviderUnavailable):
    """RunPod explicitly rejected the request without creating a Pod."""


class PodProvider(Protocol):
    def list(self, *, fresh: bool = False) -> list[Pod]: ...
    def get(self, pod_id: str) -> Pod | None: ...
    def create(self, body: dict[str, object]) -> Pod: ...
    def delete(self, pod_id: str) -> None: ...
    def rename(self, pod_id: str, name: str) -> Pod: ...


class RunPodClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        *,
        min_interval: float = 0,
        cache_seconds: float = 0,
    ) -> None:
        if not 0 <= min_interval <= 60:
            raise ValueError("Invalid RunPod request interval")
        self._api_key = api_key
        self._client = client
        self._interval, self._next = min_interval, 0.0
        self._rate = threading.Lock()
        self._cache_seconds = cache_seconds
        self._cache_lock = threading.Lock()
        self._cached: tuple[float, list[Pod]] | None = None

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
        with self._rate:
            wait = self._next - time.monotonic()
            if wait > 30:
                error = CreateRejected if method == "POST" else ProviderUnavailable
                raise error(
                    "RunPod is in a rate-limit cooldown",
                    status=429,
                    retry_after=str(int(wait) + 1),
                )
            if wait > 0:
                time.sleep(wait)
            self._next = time.monotonic() + self._interval
        try:
            response = self._client.request(
                method,
                API + path,
                json=body,
                params=params,
                headers={"Authorization": f"Bearer {self._api_key}"},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ProviderUnavailable("RunPod request did not complete") from None
        if response.status_code == 429:
            value = response.headers.get("Retry-After", "30")
            try:
                delay = (
                    max(0.0, float(value))
                    if value.replace(".", "", 1).isdigit()
                    else max(
                        0.0, parsedate_to_datetime(value).timestamp() - time.time()
                    )
                )
            except (ValueError, TypeError, OverflowError):
                delay = 30.0
            with self._rate:
                self._next = max(self._next, time.monotonic() + delay)
        return response

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
            markers = {
                key: str(environment[key]) for key in MARKERS if key in environment
            }
            tcp: dict[int, tuple[str, int]] = {}
            runtime = value.get("runtime")
            if runtime is not None:
                mappings = object_dict(runtime).get("ports", [])
                if not isinstance(mappings, list):
                    raise ValueError("Invalid TCP port mappings")
                for entry in mappings:
                    mapping = object_dict(entry)
                    if (
                        mapping.get("type") == "tcp"
                        and mapping.get("ip") is not None
                        and mapping.get("public") is not None
                    ):
                        private, public = mapping["private"], mapping["public"]
                        if (
                            type(private) is not int
                            or type(public) is not int
                            or not 1 <= public <= 65535
                        ):
                            raise ValueError("Invalid TCP port mapping")
                        address = str(ipaddress.ip_address(string(mapping["ip"])))
                        tcp[private] = (address, public)
            return Pod(
                string(value["id"]),
                string(value["name"]),
                string(value["image"]),
                string(value["status"]),
                markers,
                tcp,
                None if value.get("cost") is None else str(value["cost"]),
            )
        except (KeyError, ValueError):
            raise ProviderUnavailable("Malformed RunPod Pod") from None

    def list(self, *, fresh: bool = False) -> list[Pod]:
        with self._cache_lock:
            if (
                not fresh
                and self._cached is not None
                and time.monotonic() - self._cached[0] < self._cache_seconds
            ):
                return list(self._cached[1])
            pods = self._list()
            self._cached = (time.monotonic(), pods)
            return list(pods)

    def _list(self) -> PodList:
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
                if (
                    not isinstance(page, list)
                    or type(pagination["hasNextPage"]) is not bool
                ):
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
        self._cached = None
        try:
            response = self._request("POST", "/pods", body=body)
        except CreateRejected:
            raise
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
        self._cached = None
        response = self._request("DELETE", f"/pods/{quote(pod_id, safe='')}")
        if response.status_code not in (204, 404):
            raise self._error(response)
        # This acknowledgement alone does not establish absence. The core GETs.

    def rename(self, pod_id: str, name: str) -> Pod:
        self._cached = None
        response = self._request(
            "PATCH", f"/pods/{quote(pod_id, safe='')}", body={"name": name}
        )
        if response.status_code != 200:
            raise self._error(response)
        pod = self._pod(self._object(response))
        if pod.id != pod_id or pod.name != name:
            raise ProviderUnavailable("RunPod lifecycle update differs")
        return pod
