"""Ordinary Pod sessions and durable, deduplicated model exchanges."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import ssl
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

import httpx
from bittensor_wallet import Keypair
from pydantic import BaseModel, ConfigDict

from .calls import RequestKey, canonical, request_digest
from .delivery import ArtifactClient, retry
from .errors import WitnessError
from .gateway import SignedTransport
from .request import PROVIDERS
from .response import http_body
from .tunnel import WebSocketTunnel
from .types import Session


class CallView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: RequestKey
    exchange_id: str
    deadline: int
    phase: Literal["reserved", "assigned", "capturing", "issued", "failed"]
    start_id: str | None = None
    pod_id: str | None = None
    authorized: bool = False
    not_dispatched: bool = False
    manifest_sha256: str | None = None
    failure: str | None = None


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    call: CallView
    hotkey: str
    session: dict[str, object] | None
    completion: dict[str, object] | None = None


class PodGatewayClient:
    def __init__(
        self,
        http: httpx.Client,
        hotkey: Keypair,
        state: Path,
        delivery_timeout: float,
    ) -> None:
        self.http, self.hotkey = http, hotkey
        self.state, self.delivery_timeout = state, delivery_timeout
        self._pod_lock = threading.Lock()
        self._start_id: str | None = None
        self._allocation = SignedTransport(http, hotkey, delivery_timeout)
        self._direct: SignedTransport | None = None
        self._expires = 0
        self._connections: list[httpx.Client] = []
        self._snapshots: dict[str, Snapshot] = {}
        self._artifacts = ArtifactClient()

        self.round_number: str | None = None
        self._allocation_request: dict[str, object] | None = None
        self._allocation_round: str | None = None

    def _json(
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
        raw = retry(
            lambda: self._request_once(method, path, body, limit=limit, deadline=deadline),
            deadline=deadline,
        )
        result: object = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid Pod gateway response", source="gateway")
        return result

    def start(
        self, invocation: RequestKey, *, expected_exchange_id: str | None = None, deadline: float | None = None,
    ) -> Snapshot:
        if expected_exchange_id is None:
            self.ensure_pod(deadline=deadline)
        value: dict[str, object] = {"request": invocation.model_dump()}
        if expected_exchange_id is not None:
            value["expected_exchange_id"] = str(uuid.UUID(expected_exchange_id))
        return self._snapshot(
            self._json("POST", "/v1/calls", value, deadline=deadline)
        )

    def snapshot(self, exchange_id: str, *, deadline: float | None = None) -> Snapshot:
        return self._snapshot(
            self._json("GET", f"/v1/calls/{uuid.UUID(exchange_id)!s}", deadline=deadline)
        )

    def _snapshot(self, value: dict[str, object]) -> Snapshot:
        snapshot = Snapshot.model_validate(value)
        self._snapshots[snapshot.call.exchange_id] = snapshot
        return snapshot

    def _allocation_json(self, method: str, path: str, value: object | None = None,
                         *, deadline: float, expected_round: str | None = None) -> dict[str, object]:
        body = b"" if value is None else canonical(value)

        def attempt() -> bytes | None:
            wait = min(20, max(0, math.ceil(deadline - time.monotonic())))
            self._allocation.headers["Prefer"] = f"wait={wait}"
            return self._allocation._request_once(method, path, body, deadline=deadline)

        raw = retry(attempt, deadline=deadline)
        result = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid allocation reply", source="gateway")
        if expected_round is not None and result.get("round") != expected_round:
            raise WitnessError("The allocated round is no longer available", source="gateway", code="round_closed")
        return result

    def _attach(self, reply: dict[str, object]) -> bool:
        round_number = reply.get("round")
        if not isinstance(round_number, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round_number) is None:
            raise WitnessError("Invalid gateway round metadata", source="gateway")
        session, endpoint = reply.get("session"), reply.get("witness")
        if (not isinstance(session, dict) or session.get("phase") != "allocated"
                or not isinstance(endpoint, dict)):
            return False
        if (endpoint.get("pod_id") != session.get("pod_id") or endpoint.get("start_id") != session.get("start_id")
                or endpoint.get("server_name") != "witness.internal" or type(endpoint.get("port")) is not int
                or not 1 <= endpoint["port"] <= 65535 or type(endpoint.get("expires")) is not int
                or not isinstance(endpoint.get("access"), str) or not isinstance(endpoint.get("certificate"), str)
                or endpoint["expires"] <= time.time()):
            raise WitnessError("Invalid direct witness allocation", source="gateway")
        address = str(ipaddress.ip_address(str(endpoint["address"])))
        host = f"[{address}]" if ":" in address else address
        tls = ssl.create_default_context(cadata=endpoint["certificate"])
        http = httpx.Client(base_url=f"https://{host}:{endpoint['port']}", verify=tls,
                            timeout=30, follow_redirects=False, trust_env=False)
        service = SignedTransport(
            http, self.hotkey, self.delivery_timeout,
            headers={"Authorization": "Bearer " + endpoint["access"]},
            tls=tls, server_name="witness.internal",
        )
        self._connections.append(http)
        self._direct, self._expires = service, endpoint["expires"]
        self._start_id = str(uuid.UUID(str(session["start_id"])))
        self.round_number = round_number
        return True

    def ensure_pod(self, *, deadline: float | None = None, renew: bool = False) -> str:
        if deadline is None:
            deadline = time.monotonic() + self.delivery_timeout
        if not self._pod_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise WitnessError("Pod connection wait timed out", source="gateway", code="capture_timeout")
        try:
            if time.monotonic() >= deadline:
                raise WitnessError("Pod connection wait timed out", source="gateway", code="capture_timeout")
            if self._direct is not None and time.time() < self._expires - 30:
                assert self._start_id is not None
                return self._start_id
            expected_round = self.round_number if renew else None
            reply = None
            if renew and self._start_id is not None:
                try:
                    reply = self._allocation_json("GET", f"/v1/pods/{self._start_id}",
                                                  deadline=deadline, expected_round=expected_round)
                except WitnessError as error:
                    if error.code != "session_not_found":
                        raise
                if reply is not None:
                    previous = reply.get("session")
                    if not isinstance(previous, dict) or previous.get("phase") in ("rejected", "lost", "retired"):
                        reply = None
            if reply is None:
                reply = self._allocation_json("GET", "/v1/pods/current", deadline=deadline,
                                              expected_round=expected_round)
            allocation = self._allocation_request if self._allocation_round == reply.get("round") else None
            current = reply.get("session")
            while isinstance(current, dict) and current.get("phase") == "retiring":
                if allocation is not None and allocation["start_id"] == current.get("start_id"):
                    allocation = None
                hint = reply.get("retry_after", 1)
                if isinstance(hint, bool) or not isinstance(hint, (int, float)) or not 0 <= hint <= 86400:
                    raise WitnessError("Invalid gateway retry interval", source="gateway")
                time.sleep(min(hint, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    raise WitnessError("Pod retirement remains unresolved; retry the same connection",
                                       source="gateway", code="pod_provisioning_pending")
                reply = self._allocation_json("GET", "/v1/pods/current", deadline=deadline,
                                              expected_round=expected_round)
                current = reply.get("session")
            if not isinstance(current, dict) or current.get("phase") in ("rejected", "lost", "retired"):
                if allocation is None or (
                    isinstance(current, dict) and current.get("start_id") == allocation["start_id"]
                ):
                    allocation = {"start_id": str(uuid.uuid4()), "replaces": current.get("start_id")
                                  if isinstance(current, dict) else None}
                    self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                reply = self._allocation_json("POST", "/v1/pods/start", allocation,
                                               deadline=deadline, expected_round=expected_round)
            retiring: str | None = None
            while True:
                session = reply.get("session")
                if retiring is not None:
                    if not isinstance(session, dict) or (
                        session.get("start_id") == retiring and session.get("phase") in ("rejected", "lost", "retired")
                    ):
                        allocation = {"start_id": str(uuid.uuid4()), "replaces": retiring}
                        self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                        reply = self._allocation_json("POST", "/v1/pods/start", allocation,
                                                      deadline=deadline, expected_round=expected_round)
                        retiring = None
                        continue
                    if session.get("start_id") != retiring:
                        retiring = None
                if not isinstance(session, dict):
                    raise WitnessError("Invalid Pod allocation", source="gateway")
                start_id = str(uuid.UUID(str(session["start_id"])))
                if session.get("phase") == "retiring":
                    retiring = start_id
                if session.get("phase") in ("rejected", "lost", "retired"):
                    raise WitnessError("Pod allocation did not become ready", source="gateway", code="pod_unavailable")
                accepted = {"start_id": start_id, "replaces": session.get("replaces")}
                if allocation != accepted:
                    allocation = accepted
                    self._allocation_request, self._allocation_round = allocation, str(reply["round"])
                if self._attach(reply):
                    return start_id
                hint = reply.get("retry_after", 1)
                if isinstance(hint, bool) or not isinstance(hint, (int, float)) or not 0 <= hint <= 86400:
                    raise WitnessError("Invalid gateway retry interval", source="gateway")
                time.sleep(min(hint, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    raise WitnessError("Pod provisioning remains unresolved; retry the same connection",
                                       source="gateway", code="pod_provisioning_pending")
                reply = self._allocation_json("GET", "/v1/pods/current" if retiring is not None else
                                               f"/v1/pods/{start_id}", deadline=deadline, expected_round=expected_round)
        finally:
            self._pod_lock.release()

    def _service(self, *, deadline: float | None = None) -> SignedTransport:
        self.ensure_pod(deadline=deadline, renew=True)
        assert self._direct is not None
        return self._direct

    def _invalidate_for(self, code: object) -> bool:
        if code not in ("allocation_access_expired", "pod_not_selected"):
            return False
        self._expires = 0
        return True

    def _request_once(
        self, method: str, path: str, body: bytes = b"", *, limit: int = 32768,
        deadline: float | None = None,
    ) -> bytes | None:
        service = self._service(deadline=deadline)
        try:
            return service._request_once(method, path, body, limit=limit, deadline=deadline)
        except httpx.TransportError:
            self._expires = 0
            raise
        except WitnessError as error:
            if not self._invalidate_for(error.code):
                raise
            return self._service(deadline=deadline)._request_once(method, path, body, limit=limit, deadline=deadline)

    @contextmanager
    def tunnel(self, identifier: str, *, deadline: float | None = None) -> Iterator[WebSocketTunnel]:
        with self._service(deadline=deadline).tunnel(identifier, deadline=deadline) as tunnel:
            yield tunnel

    def close(self) -> None:
        for http in self._connections:
            http.close()
        self._artifacts.close()

    def send(self, request: httpx.Request) -> httpx.Response:
        service = self._service()
        try:
            response = service.send(request)
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

    def _download(self, link: object, limit: int) -> bytes:
        return self._artifacts.download(link, limit, delivery_timeout=self.delivery_timeout)

    def _completion(self, call: CallView) -> dict[str, object]:
        snapshot = self._snapshots.get(call.exchange_id)
        if snapshot is None or snapshot.call.phase != "issued" or snapshot.completion is None:
            snapshot = self.snapshot(call.exchange_id)
        if snapshot.call.manifest_sha256 != call.manifest_sha256 or snapshot.completion is None:
            raise WitnessError("Completion metadata is unavailable", call.exchange_id, code="invalid_recording")
        return snapshot.completion

    def get(self, identifier: str, name: str, limit: int = 16 * 1024 * 1024, *, deadline: float | None = None) -> bytes | None:
        snapshot = self.snapshot(identifier)
        if snapshot.call.phase != "issued":
            return None
        completion = self._completion(snapshot.call)
        links = completion.get("artifacts")
        if not isinstance(links, dict) or name not in links:
            raise WitnessError("Evidence object is unavailable", identifier, code="recording_unavailable")
        return self._download(links[name], limit)

    def required(
        self, exchange_id: str, name: str, *, limit: int = 16 * 1024 * 1024
    ) -> bytes:
        value = self.get(exchange_id, name, limit=limit)
        if value is None:
            raise WitnessError(
                "Recorded evidence is unavailable",
                exchange_id,
                source="gateway",
                code="recording_unavailable",
            )
        return value

    def recorded(
        self, invocation: RequestKey, call: CallView, proof_hash: str, *,
        request: bytes | None = None, raw_response: bytes | None = None,
    ) -> tuple[Session, int, bytes]:
        completion = self._completion(call)
        manifest = str(completion["manifest"]).encode()
        links = completion["artifacts"]
        if not isinstance(links, dict) or not isinstance(completion.get("session"), dict):
            raise WitnessError("Invalid completion metadata", call.exchange_id, code="invalid_recording")
        if request is None:
            request = self._download(links.get("request.json"), 8 * 1024 * 1024)
        response = (self._download(links.get("response.sse"), 16 * 1024 * 1024)
                    if raw_response is None else http_body(raw_response, allow_error=True))
        return _recorded_response(self.hotkey, invocation, call, proof_hash, completion,
                                  manifest, request, response, raw_response)


def _recorded_response(
    hotkey: Keypair, invocation: RequestKey, call: CallView, proof_hash: str,
    completion: dict[str, object], manifest: bytes, request: bytes, response: bytes,
    raw_response: bytes | None,
) -> tuple[Session, int, bytes]:
    session = cast(Session, completion["session"])
    value = json.loads(manifest)
    host, path = PROVIDERS[invocation.provider]
    expected_provider = f"https://{host}{path}"
    if (
        hashlib.sha256(manifest).hexdigest() != call.manifest_sha256
        or not isinstance(value, dict)
        or value.get("call_id") != invocation.id
        or value.get("exchange_id") != call.exchange_id
        or value.get("proof_key_sha256") != proof_hash
        or value.get("request_key") != invocation.model_dump()
        or request_digest(request) != invocation.request_sha256
        or value.get("request_sha256") != hashlib.sha256(request).hexdigest()
        or value.get("response_sha256") != hashlib.sha256(response).hexdigest()
        or session.get("id") != call.exchange_id
        or session.get("protocol") != 2
        or session.get("proof_key_sha256") != proof_hash
        or session.get("provider") != expected_provider
        or session.get("miner") != hotkey.ss58_address
        or value.get("hotkey") != hotkey.ss58_address
        or session.get("round") != value.get("round")
        or (raw_response is not None and int(raw_response.split(b" ", 2)[1]) != value.get("provider_status"))
        or type(value.get("provider_status")) is not int
    ):
        raise WitnessError(
            "Recorded evidence differs from this call",
            call.exchange_id,
            source="gateway",
            code="invalid_recording",
        )
    return session, value["provider_status"], response
