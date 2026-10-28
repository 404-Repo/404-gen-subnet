"""Native async authenticated Pod allocation, capture and evidence delivery."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import ssl
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
from bittensor_wallet import Keypair
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .auth import hotkey_headers
from .calls import RequestKey, canonical
from .delivery import AsyncArtifactClient, async_request, async_retry
from .errors import WitnessError
from .pods import CallView, Snapshot, _recorded_response
from .response import http_body
from .async_tunnel import AsyncWebSocketTunnel
from .types import Session


class AsyncSignedTransport:
    """Signed requests to one endpoint; the caller owns its HTTP connection."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        hotkey: Keypair,
        delivery_timeout: float,
        *,
        headers: dict[str, str] | None = None,
        tls: ssl.SSLContext | None = None,
        server_name: str | None = None,
    ) -> None:
        self.http, self.hotkey, self.delivery_timeout = http, hotkey, delivery_timeout
        self.headers = {} if headers is None else headers
        self.tls, self.server_name = tls, server_name

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        headers = hotkey_headers(self.hotkey, body, method=method, path=path)
        headers.update(self.headers)
        return headers

    async def _request_once(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        limit: int = 32768,
        deadline: float | None = None,
    ) -> bytes | None:
        try:
            return await async_request(
                self.http,
                method,
                path,
                body,
                headers=self._headers(method, path, body),
                limit=limit,
                deadline=deadline,
                extensions={"sni_hostname": self.server_name} if self.server_name else None,
            )
        except WitnessError as error:
            if error.status_code == 404 and error.code == "object_not_ready":
                return None
            raise

    @asynccontextmanager
    async def tunnel(
        self, identifier: str, *, deadline: float | None = None
    ) -> AsyncIterator[AsyncWebSocketTunnel]:
        path = f"/v1/exchanges/{identifier}/connect"
        url = str(self.http.base_url).rstrip("/").replace("https://", "wss://", 1) + path
        remaining = deadline - time.monotonic() if deadline is not None else 30
        try:
            if remaining <= 0:
                raise TimeoutError("Capture wait timed out before WebSocket connection")
            async with connect(
                url,
                additional_headers=self._headers("GET", path, b""),
                proxy=None,
                compression=None,
                max_size=256 * 1024,
                max_queue=16,
                open_timeout=min(30, remaining),
                close_timeout=min(5, remaining),
                ssl=self.tls,
                server_hostname=self.server_name,
            ) as socket:
                yield AsyncWebSocketTunnel(socket, identifier)
        except (WebSocketException, OSError, TimeoutError) as error:
            raise WitnessError(
                "Worker connection lost; recover the saved result",
                identifier,
                code="connection_lost",
                source="gateway",
            ) from error

    async def send(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode("ascii")
        allowed = (
            request.method == "POST"
            and request.url.path in ("/v1/render/grid", "/v1/submissions")
            or request.method == "GET"
            and (
                path in ("/v1/artifacts/context", "/v1/submissions")
                or re.fullmatch(r"/v1/submissions/[0-9a-f]{64}", path) is not None
            )
        )
        if not allowed:
            raise ValueError("Unknown Pod route")
        body = await request.aread()
        headers = self._headers(request.method, path, body)
        headers["Content-Type"] = "application/json"
        message = self.http.build_request(
            request.method, path, content=body, headers=headers, timeout=150
        )
        if "timeout" in request.extensions:
            message.extensions["timeout"] = request.extensions["timeout"]
        message.extensions["sni_hostname"] = self.server_name
        return await self.http.send(message)


class AsyncPodGatewayClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        hotkey: Keypair,
        state: Path,
        delivery_timeout: float,
    ) -> None:
        self.http, self.hotkey = http, hotkey
        self.state, self.delivery_timeout = state, delivery_timeout
        self._pod_lock = asyncio.Lock()
        self._start_id: str | None = None
        self._allocation = AsyncSignedTransport(http, hotkey, delivery_timeout)
        self._direct: AsyncSignedTransport | None = None
        self._expires = 0
        self._connections: list[httpx.AsyncClient] = []
        self._snapshots: dict[str, Snapshot] = {}
        self._artifacts = AsyncArtifactClient()

        self.round_number: str | None = None
        self._allocation_request: dict[str, object] | None = None
        self._allocation_round: str | None = None
        self._replacement_generation = 0
        self._replacement_pending = False
        self._replacement_pod: str | None = None

    async def replace_pod(self) -> None:
        await self.ensure_pod(replace=True)

    def _replacement_intent(self, round_number: str, replaces: str | None) -> dict[str, object]:
        identity = (
            f"witness-pod-replacement/v1/{self.http.base_url}/{round_number}/"
            f"{self.hotkey.ss58_address}/{replaces or 'initial'}"
        )
        return {
            "start_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
            "replaces": replaces,
        }

    async def _json(
        self,
        method: str,
        path: str,
        value: object | None = None,
        *,
        limit: int = 1024 * 1024,
        deadline: float | None = None,
    ) -> dict[str, object]:
        body = b"" if value is None else canonical(value)
        if deadline is None:
            deadline = time.monotonic() + self.delivery_timeout
        raw = await async_retry(
            lambda: self._request_once(method, path, body, limit=limit, deadline=deadline),
            deadline=deadline,
        )
        result: object = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid Pod gateway response", source="gateway")
        return result

    async def start(
        self,
        invocation: RequestKey,
        *,
        expected_exchange_id: str | None = None,
        deadline: float | None = None,
    ) -> Snapshot:
        value: dict[str, object] = {"request": invocation.model_dump()}
        if expected_exchange_id is not None:
            value["expected_exchange_id"] = str(uuid.UUID(expected_exchange_id))
        return self._snapshot(await self._json("POST", "/v1/calls", value, deadline=deadline))

    async def snapshot(self, exchange_id: str, *, deadline: float | None = None) -> Snapshot:
        return self._snapshot(
            await self._json("GET", f"/v1/calls/{uuid.UUID(exchange_id)!s}", deadline=deadline)
        )

    def _snapshot(self, value: dict[str, object]) -> Snapshot:
        snapshot = Snapshot.model_validate(value)
        self._snapshots[snapshot.call.exchange_id] = snapshot
        return snapshot

    async def _allocation_json(
        self, method: str, path: str, value: object | None = None, *, deadline: float
    ) -> dict[str, object]:
        body = b"" if value is None else canonical(value)

        async def attempt() -> bytes | None:
            wait = min(20, max(0, math.ceil(deadline - time.monotonic())))
            self._allocation.headers["Prefer"] = f"wait={wait}"
            return await self._allocation._request_once(method, path, body, deadline=deadline)

        raw = await async_retry(attempt, deadline=deadline)
        result = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid allocation reply", source="gateway")
        return result

    def _attach(self, reply: dict[str, object]) -> bool:
        round_number = reply.get("round")
        if (
            not isinstance(round_number, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round_number) is None
        ):
            raise WitnessError("Invalid gateway round metadata", source="gateway")
        session, endpoint = reply.get("session"), reply.get("witness")
        if (
            not isinstance(session, dict)
            or session.get("phase") != "allocated"
            or not isinstance(endpoint, dict)
        ):
            return False
        if (
            endpoint.get("pod_id") != session.get("pod_id")
            or endpoint.get("start_id") != session.get("start_id")
            or endpoint.get("server_name") != "witness.internal"
            or type(endpoint.get("port")) is not int
            or not 1 <= endpoint["port"] <= 65535
            or type(endpoint.get("expires")) is not int
            or not isinstance(endpoint.get("access"), str)
            or not isinstance(endpoint.get("certificate"), str)
            or endpoint["expires"] <= time.time()
        ):
            raise WitnessError("Invalid direct witness allocation", source="gateway")
        address = str(ipaddress.ip_address(str(endpoint["address"])))
        host = f"[{address}]" if ":" in address else address
        tls = ssl.create_default_context(cadata=endpoint["certificate"])
        http = httpx.AsyncClient(
            base_url=f"https://{host}:{endpoint['port']}",
            verify=tls,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )
        service = AsyncSignedTransport(
            http,
            self.hotkey,
            self.delivery_timeout,
            headers={"Authorization": "Bearer " + endpoint["access"]},
            tls=tls,
            server_name="witness.internal",
        )
        self._connections.append(http)
        self._direct, self._expires = service, endpoint["expires"]
        self._start_id = str(uuid.UUID(str(session["start_id"])))
        self.round_number = round_number
        return True

    async def ensure_pod(self, *, deadline: float | None = None, replace: bool = False) -> str:
        observed = self._start_id
        generation = self._replacement_generation
        if deadline is None:
            deadline = time.monotonic() + self.delivery_timeout
        try:
            await asyncio.wait_for(self._pod_lock.acquire(), max(0, deadline - time.monotonic()))
        except TimeoutError:
            raise WitnessError(
                "Pod connection wait timed out", source="gateway", code="capture_timeout"
            )
        try:
            if time.monotonic() >= deadline:
                raise WitnessError(
                    "Pod connection wait timed out", source="gateway", code="capture_timeout"
                )
            replace = replace and generation == self._replacement_generation
            if (
                not replace and not self._replacement_pending
                and self._direct is not None and time.time() < self._expires - 30
            ):
                assert self._start_id is not None
                return self._start_id
            reply = await self._allocation_json("GET", "/v1/pods/current", deadline=deadline)
            allocation = self._allocation_request if self._allocation_round == reply.get("round") else None
            current = reply.get("session")
            continuing = self._replacement_pending
            if replace:
                if self.round_number is not None and reply.get("round") != self.round_number:
                    raise WitnessError("Gateway round changed before replacement", code="round_mismatch", source="gateway")
                self._replacement_generation += 1
                self._replacement_pending = True
                self._direct, self._expires = None, 0
            if isinstance(current, dict) and current.get("phase") == "allocated" and (
                replace and not continuing and (observed is None or current.get("start_id") == observed)
                or continuing and allocation is not None
                and current.get("start_id") == allocation.get("replaces")
            ):
                old_start = str(uuid.UUID(str(current["start_id"])))
                if allocation is None or allocation.get("replaces") != old_start:
                    allocation = self._replacement_intent(str(reply["round"]), old_start)
                    self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                self._replacement_pod = str(current["pod_id"])
                self._direct, self._expires = None, 0
                reply = await self._allocation_json("POST", "/v1/pods/start", allocation, deadline=deadline)
                current = reply.get("session")
            elif not isinstance(current, dict) or current.get("phase") in (
                "rejected",
                "lost",
                "retired",
            ):
                if allocation is None or (
                    isinstance(current, dict) and current.get("start_id") == allocation["start_id"]
                ):
                    previous = current.get("start_id") if isinstance(current, dict) else None
                    allocation = (
                        self._replacement_intent(str(reply["round"]), str(previous) if previous else None)
                        if replace else {"start_id": str(uuid.uuid4()), "replaces": previous}
                    )
                    self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                reply = await self._allocation_json(
                    "POST", "/v1/pods/start", allocation, deadline=deadline
                )
            retiring: str | None = None
            while True:
                session = reply.get("session")
                if retiring is not None and (not isinstance(session, dict) or (
                    session.get("start_id") == retiring and session.get("phase") in ("rejected", "lost", "retired")
                )):
                    allocation = self._replacement_intent(str(reply["round"]), retiring)
                    self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                    reply = await self._allocation_json("POST", "/v1/pods/start", allocation, deadline=deadline)
                    retiring = None
                    continue
                if not isinstance(session, dict):
                    raise WitnessError("Invalid Pod allocation", source="gateway")
                start_id = str(uuid.UUID(str(session["start_id"])))
                if self._replacement_pending and session.get("phase") == "retiring":
                    retiring = start_id
                if session.get("phase") in ("rejected", "lost", "retired"):
                    raise WitnessError(
                        "Pod allocation did not become ready",
                        source="gateway",
                        code="pod_unavailable",
                    )
                accepted = {"start_id": start_id, "replaces": session.get("replaces")}
                if allocation != accepted:
                    allocation = accepted
                    self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                if self._attach(reply):
                    if self._replacement_pod is not None and session.get("pod_id") == self._replacement_pod:
                        self._direct, self._expires = None, 0
                        raise WitnessError("Gateway returned the Pod being replaced", code="pod_replacement_unconfirmed", source="gateway")
                    self._replacement_pod = None
                    self._replacement_pending = False
                    return start_id
                hint = reply.get("retry_after", 1)
                if (
                    isinstance(hint, bool)
                    or not isinstance(hint, (int, float))
                    or not 0 <= hint <= 86400
                ):
                    raise WitnessError("Invalid gateway retry interval", source="gateway")
                await asyncio.sleep(min(hint, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    raise WitnessError(
                        "Pod provisioning remains unresolved; retry the same connection",
                        source="gateway",
                        code="pod_provisioning_pending",
                    )
                reply = await self._allocation_json(
                    "GET", "/v1/pods/current" if retiring is not None else f"/v1/pods/{start_id}", deadline=deadline
                )
        finally:
            self._pod_lock.release()

    async def _service(self, *, deadline: float | None = None) -> AsyncSignedTransport:
        await self.ensure_pod(deadline=deadline)
        assert self._direct is not None
        return self._direct

    def _invalidate_for(self, code: object) -> bool:
        if code not in ("allocation_access_expired", "pod_not_selected"):
            return False
        self._expires = 0
        return True

    async def _request_once(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        limit: int = 32768,
        deadline: float | None = None,
    ) -> bytes | None:
        service = await self._service(deadline=deadline)
        try:
            return await service._request_once(method, path, body, limit=limit, deadline=deadline)
        except httpx.TransportError:
            self._expires = 0
            raise
        except WitnessError as error:
            if not self._invalidate_for(error.code):
                raise
            service = await self._service(deadline=deadline)
            return await service._request_once(method, path, body, limit=limit, deadline=deadline)

    @asynccontextmanager
    async def tunnel(
        self, identifier: str, *, deadline: float | None = None
    ) -> AsyncIterator[AsyncWebSocketTunnel]:
        service = await self._service(deadline=deadline)
        async with service.tunnel(identifier, deadline=deadline) as tunnel:
            yield tunnel

    async def aclose(self) -> None:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(self._artifacts.aclose)
            for http in self._connections:
                cleanup.push_async_callback(http.aclose)

    async def send(self, request: httpx.Request) -> httpx.Response:
        service = await self._service()
        try:
            response = await service.send(request)
            if response.status_code in (401, 409):
                try:
                    error = response.json()
                except ValueError:
                    pass
                else:
                    if isinstance(error, dict):
                        self._invalidate_for(error.get("error"))
            return response
        except httpx.TransportError:
            self._expires = 0
            raise

    async def _download(self, link: object, limit: int) -> bytes:
        return await self._artifacts.download(link, limit, delivery_timeout=self.delivery_timeout)

    async def _completion(self, call: CallView) -> dict[str, object]:
        snapshot = self._snapshots.get(call.exchange_id)
        if snapshot is None or snapshot.call.phase != "issued" or snapshot.completion is None:
            snapshot = await self.snapshot(call.exchange_id)
        if snapshot.call.manifest_sha256 != call.manifest_sha256 or snapshot.completion is None:
            raise WitnessError(
                "Completion metadata is unavailable", call.exchange_id, code="invalid_recording"
            )
        return snapshot.completion

    async def get(
        self,
        identifier: str,
        name: str,
        limit: int = 16 * 1024 * 1024,
        *,
        deadline: float | None = None,
    ) -> bytes | None:
        snapshot = await self.snapshot(identifier)
        if snapshot.call.phase != "issued":
            return None
        completion = await self._completion(snapshot.call)
        links = completion.get("artifacts")
        if not isinstance(links, dict) or name not in links:
            raise WitnessError(
                "Evidence object is unavailable", identifier, code="recording_unavailable"
            )
        return await self._download(links[name], limit)

    async def required(
        self, exchange_id: str, name: str, *, limit: int = 16 * 1024 * 1024
    ) -> bytes:
        value = await self.get(exchange_id, name, limit=limit)
        if value is None:
            raise WitnessError(
                "Recorded evidence is unavailable",
                exchange_id,
                source="gateway",
                code="recording_unavailable",
            )
        return value

    async def recorded(
        self,
        invocation: RequestKey,
        call: CallView,
        proof_hash: str,
        *,
        request: bytes | None = None,
        raw_response: bytes | None = None,
    ) -> tuple[Session, int, bytes]:
        completion = await self._completion(call)
        manifest = str(completion["manifest"]).encode()
        links = completion["artifacts"]
        if not isinstance(links, dict) or not isinstance(completion.get("session"), dict):
            raise WitnessError(
                "Invalid completion metadata", call.exchange_id, code="invalid_recording"
            )
        if request is None:
            request = await self._download(links.get("request.json"), 8 * 1024 * 1024)
        response = (
            await self._download(links.get("response.sse"), 16 * 1024 * 1024)
            if raw_response is None
            else http_body(raw_response, allow_error=True)
        )
        return _recorded_response(
            self.hotkey,
            invocation,
            call,
            proof_hash,
            completion,
            manifest,
            request,
            response,
            raw_response,
        )
