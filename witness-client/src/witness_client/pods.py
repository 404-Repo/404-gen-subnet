"""Ordinary Pod sessions and durable, deduplicated model exchanges."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, cast

import httpx
from bittensor_wallet import Keypair
from pydantic import BaseModel, ConfigDict

from .calls import Generation, Invocation, RequestKey, canonical, request_digest
from .errors import WitnessError
from .gateway import GatewayClient
from .request import PROVIDERS
from .state import save
from .types import Session


class CallView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: RequestKey
    exchange_id: str
    owner_ordinal: int
    deadline: int
    phase: Literal["reserved", "assigned", "capturing", "issued", "failed"]
    start_id: str | None = None
    pod_id: str | None = None
    authorized: bool = False
    manifest_sha256: str | None = None
    failure: str | None = None


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    call: CallView
    round: str
    hotkey: str
    session: dict[str, object] | None


class EntryView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call_id: str
    delivery: int | None


class RecordingView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    revision: str
    scope_key: str
    generation: Generation
    entries: list[EntryView]
    calls: dict[str, CallView]
    output_sha256: str | None
    output_is_none: bool
    finished: bool


class PodGatewayClient(GatewayClient):
    def __init__(
        self,
        http: httpx.Client,
        hotkey: Keypair,
        state: Path,
        delivery_timeout: float,
        *,
        round_number: str,
    ) -> None:
        super().__init__(http, hotkey, state, delivery_timeout)
        self._pod_lock = threading.Lock()
        self._start_id: str | None = None
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
        raw = self._request_with_retry(
            method, path, b"" if value is None else canonical(value), limit=limit
        )
        result: object = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise WitnessError("Invalid Pod gateway response", source="gateway")
        return result

    def reserve(self, invocation: Invocation) -> Snapshot:
        return Snapshot.model_validate(
            self._json("POST", "/calls", invocation.model_dump())
        )

    def snapshot(self, exchange_id: str) -> Snapshot:
        return Snapshot.model_validate(
            self._json("GET", f"/status/{uuid.UUID(exchange_id)!s}")
        )

    def dispatch(self, invocation: Invocation, start_id: str) -> Snapshot:
        return Snapshot.model_validate(
            self._json(
                "POST",
                "/calls/dispatch",
                {"invocation": invocation.model_dump(), "start_id": start_id},
            )
        )

    def ensure_pod(self) -> str:
        with self._pod_lock:
            if self._start_id is not None:
                reply = self._json("GET", f"/pods/{self._start_id}")
                session = reply.get("session")
                if isinstance(session, dict) and session.get("phase") == "allocated":
                    return self._start_id
                self._start_id = None
            pending: dict[str, object] | None = None
            if self._pending.is_file():
                value = json.loads(self._pending.read_bytes())
                if isinstance(value, dict) and value.get("pending") is True:
                    pending = value
            if pending is None:
                reply = self._json("GET", "/pods/current")
                if reply.get("round") != self.round_number:
                    raise WitnessError(
                        "Gateway competition round changed",
                        code="round_changed",
                        source="gateway",
                    )
                current = reply.get("session")
                if isinstance(current, dict) and current.get("phase") in (
                    "draining",
                    "creating",
                    "retiring",
                ):
                    # Join an unresolved previous connect, even after local state loss.
                    pending = {
                        "start_id": current["start_id"],
                        "replaces": current.get("replaces"),
                        "pending": True,
                    }
                else:
                    pending = {
                        "start_id": str(uuid.uuid4()),
                        "replaces": current.get("start_id")
                        if isinstance(current, dict)
                        else None,
                        "pending": True,
                    }
                save(self._pending, canonical(pending))
            start_id = str(uuid.UUID(str(pending["start_id"])))
            self._json(
                "POST",
                "/pods/start",
                {
                    "round": self.round_number,
                    "start_id": start_id,
                    "replaces": pending.get("replaces"),
                },
            )
            deadline = time.monotonic() + self.delivery_timeout
            while True:
                reply = self._json("GET", f"/pods/{start_id}")
                session = reply.get("session")
                if not isinstance(session, dict):
                    raise WitnessError("Invalid Pod allocation", source="gateway")
                if session.get("phase") == "allocated" and reply.get("ready") is True:
                    self._start_id = start_id
                    save(self._pending, canonical({**pending, "pending": False}))
                    return start_id
                if session.get("phase") in ("rejected", "lost", "retired"):
                    save(self._pending, canonical({**pending, "pending": False}))
                    raise WitnessError(
                        "Pod allocation did not become ready",
                        source="gateway",
                        code="pod_unavailable",
                    )
                if time.monotonic() >= deadline:
                    raise WitnessError(
                        "Pod provisioning remains unresolved; retry the same connection",
                        source="gateway",
                        code="pod_provisioning_pending",
                    )
                time.sleep(min(5, max(0, deadline - time.monotonic())))

    def delivered(self, invocation: Invocation) -> int:
        result = self._json("POST", "/calls/deliver", invocation.model_dump())
        value = result.get("delivery")
        if type(value) is not int:
            raise WitnessError("Invalid delivery receipt", source="gateway")
        return value

    def finish_generation(
        self, generation: Generation, count: int, output: str | None
    ) -> None:
        self._json(
            "POST",
            "/generations/finish",
            {
                "generation": generation.model_dump(),
                "count": count,
                "output_sha256": hashlib.sha256((output or "").encode()).hexdigest(),
                "output_is_none": output is None,
            },
        )

    def recording(self, generation: Generation) -> RecordingView:
        return RecordingView.model_validate(
            self._json("GET", f"/recordings/{generation.id}")
        )

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
        self, invocation: Invocation, call: CallView, proof_hash: str
    ) -> tuple[Session, int, bytes]:
        manifest = self.required(call.exchange_id, "manifest.json", limit=1024 * 1024)
        request = self.required(call.exchange_id, "request.json", limit=8 * 1024 * 1024)
        response = self.required(call.exchange_id, "response.sse")
        session = cast(
            Session,
            json.loads(self.required(call.exchange_id, "session.json", limit=4096)),
        )
        value = json.loads(manifest)
        host, path = PROVIDERS[invocation.request.provider]
        expected_provider = f"https://{host}{path}"
        if (
            hashlib.sha256(manifest).hexdigest() != call.manifest_sha256
            or not isinstance(value, dict)
            or value.get("generation_id") != invocation.generation.id
            or value.get("call_id") != invocation.request.id
            or value.get("exchange_id") != call.exchange_id
            or value.get("proof_key_sha256") != proof_hash
            or value.get("request_key") != invocation.request.model_dump()
            or request_digest(request) != invocation.request.request_sha256
            or value.get("request_sha256") != hashlib.sha256(request).hexdigest()
            or value.get("response_sha256") != hashlib.sha256(response).hexdigest()
            or session.get("id") != call.exchange_id
            or session.get("protocol") != 2
            or session.get("proof_key_sha256") != proof_hash
            or session.get("provider") != expected_provider
            or session.get("miner") != self.hotkey.ss58_address
            or value.get("hotkey") != self.hotkey.ss58_address
            or session.get("round") != value.get("round")
            or type(value.get("provider_status")) is not int
        ):
            raise WitnessError(
                "Recorded evidence differs from this call",
                call.exchange_id,
                source="gateway",
                code="invalid_recording",
            )
        return session, value["provider_status"], response
