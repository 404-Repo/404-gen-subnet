"""Ordinary Pod sessions and durable, deduplicated model exchanges."""

from __future__ import annotations

import hashlib
import ipaddress
import json
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
from .state import remaining_seconds, save
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
        *,
        round_number: str,
    ) -> None:
        self.http, self.hotkey = http, hotkey
        self.state, self.delivery_timeout = state, delivery_timeout
        self._pod_lock = threading.Lock()
        self._start_id: str | None = None
        self._unselected: str | None = None
        self._allocation = SignedTransport(http, hotkey, delivery_timeout)
        self._direct: SignedTransport | None = None
        self._expires = 0
        self._connections: list[httpx.Client] = []
        self._snapshots: dict[str, Snapshot] = {}
        self._artifacts = ArtifactClient()

        self.round_number = round_number
        digest = hashlib.sha256(
            f"{self.http.base_url}/{self.hotkey.ss58_address}/{round_number}".encode()
        ).hexdigest()[:24]
        self._pending = self.state / f"pod-connect-{digest}.json"

    def _json(
        self,
        method: str,
        path: str,
        value: object | None = None,
        *,
        limit: int = 1024 * 1024,
    ) -> dict[str, object]:
        body = b"" if value is None else canonical(value)
        raw = retry(
            lambda: self._request_once(method, path, body, limit=limit),
            deadline=time.monotonic() + self.delivery_timeout,
        )
        result: object = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid Pod gateway response", source="gateway")
        return result

    def reserve(self, invocation: RequestKey) -> Snapshot:
        return self._snapshot(
            self._json("POST", "/calls", invocation.model_dump())
        )

    def snapshot(self, exchange_id: str) -> Snapshot:
        return self._snapshot(
            self._json("GET", f"/status/{uuid.UUID(exchange_id)!s}")
        )

    def dispatch(self, invocation: RequestKey, start_id: str) -> Snapshot:
        return self._snapshot(
            self._json(
                "POST",
                "/calls/dispatch",
                {"request": invocation.model_dump(), "start_id": start_id},
            )
        )

    def retry(self, invocation: RequestKey, exchange_id: str) -> Snapshot:
        return self._snapshot(
            self._json("POST", "/calls/retry", {
                "request": invocation.model_dump(), "exchange_id": exchange_id,
            })
        )

    def _snapshot(self, value: dict[str, object]) -> Snapshot:
        snapshot = Snapshot.model_validate(value)
        self._snapshots[snapshot.call.exchange_id] = snapshot
        return snapshot

    def _allocation_json(self, method: str, path: str, value: object | None = None) -> dict[str, object]:
        raw = self._allocation._request_with_retry(method, path, b"" if value is None else canonical(value))
        result = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid allocation reply", source="gateway")
        return result

    def _attach(self, reply: dict[str, object]) -> bool:
        if reply.get("round") != self.round_number:
            raise WitnessError("Gateway competition round changed", code="round_changed", source="gateway")
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
        self._unselected = None
        return True

    def ensure_pod(self) -> str:
        with self._pod_lock:
            if self._direct is not None and time.time() < self._expires - 30:
                assert self._start_id is not None
                return self._start_id
            pending: dict[str, object] | None = None
            posted: dict[str, object] | None = None
            if self._pending.is_file():
                value = json.loads(self._pending.read_bytes())
                if isinstance(value, dict) and value.get("pending") is True:
                    pending = value
                    posted = self._allocation_json("POST", "/pods/start", {"round": self.round_number,
                                                   "start_id": pending["start_id"], "replaces": pending.get("replaces")})
            if pending is None:
                reply = self._allocation_json("GET", "/pods/current")
                refresh_deadline = time.monotonic() + self.delivery_timeout
                if (self._unselected is not None and isinstance(reply.get("session"), dict) and
                        cast(dict[str, object], reply["session"]).get("start_id") == self._unselected and
                        cast(dict[str, object], reply["session"]).get("phase") == "allocated"):
                    remaining = refresh_deadline - time.monotonic()
                    if remaining <= 0:
                        raise WitnessError("Pod selection remains unresolved; retry the same connection",
                                           source="gateway", code="pod_provisioning_pending")
                    delay = reply.get("retry_after", 1)
                    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 < delay <= 86400:
                        raise WitnessError("Invalid gateway retry interval", source="gateway")
                    time.sleep(min(delay, remaining))
                    if time.monotonic() >= refresh_deadline:
                        raise WitnessError("Pod selection remains unresolved; retry the same connection",
                                           source="gateway", code="pod_provisioning_pending")
                    reply = self._allocation_json("GET", "/pods/current")
                if reply.get("round") != self.round_number:
                    raise WitnessError("Gateway competition round changed", code="round_changed", source="gateway")
                if self._attach(reply):
                    assert self._start_id is not None
                    return self._start_id
                current = reply.get("session")
                if isinstance(current, dict) and current.get("phase") in ("queued", "allocated", "draining", "creating", "retiring"):
                    pending = {"start_id": current["start_id"], "replaces": current.get("replaces"), "pending": True}
                else:
                    pending = {"start_id": str(uuid.uuid4()), "replaces": current.get("start_id")
                               if isinstance(current, dict) else None, "pending": True}
                    save(self._pending, canonical(pending))
                    posted = self._allocation_json("POST", "/pods/start", {"round": self.round_number,
                                                   "start_id": pending["start_id"], "replaces": pending.get("replaces")})
            if posted is not None:
                if posted.get("round") != self.round_number:
                    raise WitnessError("Gateway competition round changed", code="round_changed", source="gateway")
                accepted = posted.get("session")
                if not isinstance(accepted, dict):
                    raise WitnessError("Invalid Pod allocation", source="gateway")
                pending = {"start_id": str(uuid.UUID(str(accepted["start_id"]))),
                           "replaces": accepted.get("replaces"), "pending": True}
                save(self._pending, canonical(pending))
                if self._attach(posted):
                    save(self._pending, canonical({**pending, "pending": False}))
                    return str(pending["start_id"])
            start_id = str(uuid.UUID(str(pending["start_id"])))
            deadline = time.monotonic() + self.delivery_timeout
            initial_delay = posted.get("retry_after", 0) if posted is not None else 0
            if isinstance(initial_delay, bool) or not isinstance(initial_delay, (int, float)) or not 0 <= initial_delay <= 86400:
                raise WitnessError("Invalid gateway retry interval", source="gateway")
            if initial_delay:
                time.sleep(min(initial_delay, max(0, deadline - time.monotonic())))
            while True:
                if initial_delay and time.monotonic() >= deadline:
                    raise WitnessError("Pod provisioning remains unresolved; retry the same connection",
                                       source="gateway", code="pod_provisioning_pending")
                reply = self._allocation_json("GET", f"/pods/{start_id}")
                session = reply.get("session")
                if not isinstance(session, dict):
                    raise WitnessError("Invalid Pod allocation", source="gateway")
                if self._attach(reply):
                    save(self._pending, canonical({**pending, "pending": False}))
                    return start_id
                if session.get("phase") in ("rejected", "lost", "retired"):
                    save(self._pending, canonical({**pending, "pending": False}))
                    raise WitnessError("Pod allocation did not become ready", source="gateway", code="pod_unavailable")
                if time.monotonic() >= deadline:
                    raise WitnessError("Pod provisioning remains unresolved; retry the same connection",
                                       source="gateway", code="pod_provisioning_pending")
                hint = reply.get("retry_after", 1)
                if isinstance(hint, bool) or not isinstance(hint, (int, float)) or not 0 < hint <= 86400:
                    raise WitnessError("Invalid gateway retry interval", source="gateway")
                time.sleep(min(hint, max(0, deadline - time.monotonic())))

    def _service(self) -> SignedTransport:
        self.ensure_pod()
        assert self._direct is not None
        return self._direct

    def _invalidate_for(self, code: object) -> bool:
        if code not in ("allocation_access_expired", "pod_not_selected"):
            return False
        self._expires = 0
        if code == "pod_not_selected":
            self._unselected = self._start_id
        return True

    def release(self, identifier: str) -> None:
        retry(
            lambda: self._request_once("POST", f"/release/{identifier}"),
            deadline=time.monotonic() + self.delivery_timeout,
        )

    def remaining_seconds(self, identifier: str) -> float:
        return remaining_seconds(self.state, identifier)

    def _request_once(self, method: str, path: str, body: bytes = b"", *, limit: int = 32768) -> bytes | None:
        service = self._service()
        try:
            return service._request_once(method, path, body, limit=limit)
        except httpx.TransportError:
            self._expires = 0
            raise
        except WitnessError as error:
            if not self._invalidate_for(error.code):
                raise
            return self._service()._request_once(method, path, body, limit=limit)

    @contextmanager
    def tunnel(self, identifier: str) -> Iterator[WebSocketTunnel]:
        with self._service().tunnel(identifier) as tunnel:
            yield tunnel

    def close(self) -> None:
        for http in self._connections:
            http.close()
        self._artifacts.close()

    def render(self, request: httpx.Request) -> httpx.Response:
        service = self._service()
        try:
            response = service.render(request)
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
            or session.get("miner") != self.hotkey.ss58_address
            or value.get("hotkey") != self.hotkey.ss58_address
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
