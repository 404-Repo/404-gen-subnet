from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from bittensor_wallet import Keypair
from miner_runtime.observations import ReplayMismatch
from miner_runtime.runner import run_generation
from support import MemoryState
from test_recording import PRICING, SCOPE
from witness_client import AsyncWitnessClient, WitnessClient, WitnessError
from witness_client.calls import RequestKey, canonical
from witness_client.client import CapturedResponse
from witness_client.pods import PodGatewayClient
from witness_pod.budget import Budget
from witness_pod.records import CallRecords
from witness_pod.recovery import response_failure

START = "00000000-0000-4000-8000-000000000001"


class Capture(WitnessClient):
    def __init__(self, directory: Path, *, pricing: bytes = PRICING) -> None:
        self._state = directory / "state"
        self._state.mkdir(mode=0o700)
        self._mode = "api"
        self._http = cast(httpx.Client, SimpleNamespace(base_url="https://fixture.test"))
        self._hotkey = cast(Keypair, SimpleNamespace(ss58_address=SCOPE.hotkey))
        self._config = {"witness_url": "https://fixture.test", "proof_parameters": "unused"}
        self._gateway = cast(PodGatewayClient, SimpleNamespace(delivery_timeout=2, round_number=SCOPE.round, ensure_pod=lambda: START))
        self._calls_lock, self._calls = threading.Lock(), {}
        self._call_slots = None
        self.state = MemoryState()
        self.records, self.budget = CallRecords(self.state), Budget(self.state, pricing)
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
                raise WitnessError("Unusable provider response", call.exchange_id, code="capture_failed", source="gateway")
        result = CapturedResponse(call.exchange_id, status, response)
        self.captured[call.exchange_id] = result
        if failure in ("delivery", "timeout"):
            raise WitnessError("Delivery unavailable", call.exchange_id, source="gateway",
                               code="capture_timeout" if failure == "timeout" else "delivery_failed")
        return result

    def save_failure(self, key: RequestKey) -> None:
        recorded = self.records.load(SCOPE, key.id)
        assert recorded is not None
        call = recorded.call
        self.state.put(
            f"rounds/{SCOPE.round}/{SCOPE.hotkey}/{call.exchange_id}/failure.json",
            canonical({"status": "failed", "error_type": call.failure}),
        )


@pytest.mark.parametrize("operation", ["submit", "get_submitted", "resume"])
def test_async_entry_keeps_live_runner_operations_outside_generation(tmp_path: Path, operation: str) -> None:
    live = Capture(tmp_path)

    async def generate(image: bytes, seed: int, witness: AsyncWitnessClient) -> None:
        if operation == "submit":
            await witness.submit(image, None)
        elif operation == "get_submitted":
            await witness.get_submitted()
        else:
            await witness.resume("exchange")

    with pytest.raises(ReplayMismatch):
        run_generation(generate, b"PNG", 42, live, None)
    assert live.paid == 0
