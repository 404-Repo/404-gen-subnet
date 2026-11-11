"""Existing recording fixtures used by the manual replay checks."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
from bittensor_wallet import Keypair
from subnet_common.witness.models import Scope
from subnet_common.witness.state import Snapshot, StateConflict
from witness_client import CapturedResponse, WitnessError
from witness_client._core import _ClientState
from witness_client.calls import RequestKey, canonical
from witness_client.pods import PodGatewayClient
from witness_pod.budget import Budget
from witness_pod.records import CallRecords
from witness_pod.recovery import response_failure


SCOPE = Scope("competition", "test-0", "5" * 48)


PRICING = canonical(
    {
        "calls_per_prompt": 2,
        "credits_per_prompt": 5_000_000,
        "long_context_above_input_tokens": 272000,
        "models": {
            "fixture": {
                "input_credits_per_token": 0.1,
                "output_credits_per_token": 0.5,
                "long_input_credits_per_token": 0.2,
                "long_output_credits_per_token": 0.75,
            }
        },
    }
)


START = "00000000-0000-4000-8000-000000000001"


class MemoryState:
    def __init__(self) -> None:
        self.objects: dict[str, Snapshot] = {}
        self.lock = threading.RLock()
        self.revision = 0

    def load(self, key: str) -> Snapshot | None:
        with self.lock:
            return self.objects.get(key)

    def save(self, key: str, data: bytes, expected: str | None) -> Snapshot:
        with self.lock:
            current = self.objects.get(key)
            if (current.etag if current else None) != expected:
                raise StateConflict("Concurrent fixture update")
            self.revision += 1
            snapshot = Snapshot(str(self.revision), data)
            self.objects[key] = snapshot
            return snapshot

    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        saved = self.load(key)
        if saved is None:
            return None
        if len(saved.data) > limit:
            raise ValueError("Oversized fixture")
        return saved.data

    def get(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None:
        return self.read(key, limit)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self.lock:
            current = self.load(key)
            self.save(key, data, current.etag if current else None)

    def delete(self, key: str) -> None:
        with self.lock:
            self.objects.pop(key, None)

    def keys(self, prefix: str) -> Iterator[str]:
        with self.lock:
            keys = sorted(key for key in self.objects if key.startswith(prefix))
        yield from keys


class Capture(_ClientState):
    def __init__(self, directory: Path, *, pricing: bytes = PRICING) -> None:
        self._state = directory / "state"
        self._state.mkdir(mode=0o700)
        self._mode = "api"
        self._http = cast(httpx.Client, SimpleNamespace(base_url="https://fixture.test"))
        self._hotkey = cast(Keypair, SimpleNamespace(ss58_address=SCOPE.hotkey))
        self._config = {"witness_url": "https://fixture.test", "proof_parameters": "unused"}
        self._gateway = cast(
            PodGatewayClient, SimpleNamespace(delivery_timeout=2, round_number=SCOPE.round, ensure_pod=lambda: START)
        )
        self.state = MemoryState()
        self.records, self.budget = CallRecords(self.state), Budget(self.state, pricing)
        self.pricing = pricing
        self.paid = 0
        self.captured: dict[str, CapturedResponse] = {}
        self.outcome: tuple[int, bytes, dict[str, int] | None] | None = None
        self.failures: dict[int, str] = {}
        self.gate: threading.Event | None = None

    def saved_response(self, exchange_id: str) -> CapturedResponse:
        return self.captured[exchange_id]

    def _capture(self, key: RequestKey, body: bytes, session_id: str | None) -> CapturedResponse:
        call = self.records.reserve(SCOPE, key)
        if call.phase == "issued":
            return self.saved_response(call.exchange_id)
        if call.phase == "failed":
            call = self.records.retry(SCOPE, key, call.exchange_id)
        assigned = self.records.assign(SCOPE, key, START, "pod")
        assert self.records.claim(SCOPE, key, "pod", assigned.capture_id or "")
        failure = self.failures.pop(key.request_id, "")
        if failure == "before":
            self.records.failed(SCOPE, key, "pod", "BeforeDispatch", exchange_id=call.exchange_id, not_dispatched=True)
            self.save_failure(key)
            raise WitnessError("Unavailable before dispatch", call.exchange_id, source="gateway", code="capture_failed")
        self.budget.reserve(SCOPE, key, call.exchange_id, body)
        self.records.authorize(SCOPE, key, "pod", exchange_id=call.exchange_id)
        self.paid += 1
        if self.gate is not None and key.request_id == 0:
            assert self.gate.wait(2)
        if failure == "unknown":
            self.budget.discard(SCOPE, key, call.exchange_id)
            self.records.failed(SCOPE, key, "pod", "Interrupted", exchange_id=call.exchange_id)
            self.save_failure(key)
            raise WitnessError("Provider outcome lost", call.exchange_id, source="gateway", code="capture_failed")
        usage = None if failure == "provider" else {"input_tokens": 10, "output_tokens": 2}
        status = 429 if failure == "provider" else 200
        terminal = failure if failure in ("failed", "incomplete") else "completed"
        response = (
            canonical({"error": {"message": "Rate limited", "code": "rate_limit_exceeded"}})
            if status != 200
            else b"data: "
            + canonical(
                {
                    "type": "response." + terminal,
                    "response": {
                        "id": "resp_fixture",
                        "status": terminal,
                        "output": [
                            {"type": "message", "content": [{"type": "output_text", "text": str(key.request_id)}]}
                        ],
                        "usage": usage,
                    },
                }
            )
            + b"\n\n"
        )
        if self.outcome is not None:
            status, response, usage = self.outcome
        manifest = canonical(
            {
                "call_id": key.id,
                "request_key": key.model_dump(),
                "round": SCOPE.round,
                "hotkey": SCOPE.hotkey,
                "exchange_id": call.exchange_id,
                "proof_key_sha256": "c" * 64,
                "pricing_sha256": self.budget.pricing_hash,
                "provider_status": status,
                "usage": usage,
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "response_sha256": hashlib.sha256(response).hexdigest(),
            }
        )
        prefix = f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{call.exchange_id}/"
        for name, content in {
            "request.json": body,
            "response.sse": response,
            "manifest.json": manifest,
            "client.bin": b"fixture",
            "server.bin": b"fixture",
            "authorization-server.bin": b"fixture",
            "claim.json": b"{}",
            "session.json": b"{}",
        }.items():
            self.state.put(prefix + name, content)
        rejected = response_failure(json.loads(manifest), response)
        if rejected is None:
            self.budget.settle(SCOPE, key, usage)
            self.records.issued(SCOPE, key, "pod", manifest, exchange_id=call.exchange_id)
        else:
            self.budget.discard(SCOPE, key, call.exchange_id)
            self.records.failed(SCOPE, key, "pod", rejected, exchange_id=call.exchange_id, manifest=manifest)
            self.save_failure(key)
            if status == 200:
                raise WitnessError(
                    "Unusable provider response", call.exchange_id, code="capture_failed", source="gateway"
                )
        result = CapturedResponse(call.exchange_id, status, response)
        self.captured[call.exchange_id] = result
        if failure in ("delivery", "timeout"):
            raise WitnessError(
                "Delivery unavailable",
                call.exchange_id,
                source="gateway",
                code="capture_timeout" if failure == "timeout" else "delivery_failed",
            )
        return result

    def save_failure(self, key: RequestKey) -> None:
        recorded = self.records.load(SCOPE, key.id)
        assert recorded is not None
        call = recorded.call
        self.state.put(
            f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{call.exchange_id}/failure.json",
            canonical({"status": "failed", "error_type": call.failure}),
        )
