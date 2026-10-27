from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import openai
import pytest
from bittensor_wallet import Keypair
from miner_runtime.accounting import BudgetRejected
from miner_runtime.observations import Inputs, ReplayMismatch
from miner_runtime.runner import run_generation
from miner_runtime.validator import ReplayWitness, run_replay
from support import MemoryState
from test_recording import PRICING, SCOPE
from witness_client import WitnessClient, WitnessError
from witness_client.calls import RequestKey, canonical
from witness_client.client import CapturedResponse
from witness_client.pods import PodGatewayClient

from pod_gateway.budget import Budget
from pod_gateway.records import CallRecords
from pod_gateway.replay import export_bundle

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
        self.pricing = pricing
        self.paid = 0
        self.failures: dict[int, str] = {}
        self.gate: threading.Event | None = None

    def _capture(self, key: RequestKey, body: bytes, session_id: str | None) -> CapturedResponse:
        call = self.records.reserve(SCOPE, key)
        if call.phase == "issued":
            return self._saved_response(call.exchange_id)
        assigned = self.records.assign(SCOPE, key, START, "pod")
        assert self.records.claim(SCOPE, key, "pod", assigned.capture_id or "")
        failure = self.failures.pop(key.request_id, "")
        if failure == "before":
            self.records.failed(SCOPE, key, "pod", "BeforeDispatch", exchange_id=call.exchange_id, not_dispatched=True)
            raise WitnessError("Unavailable before dispatch", call.exchange_id, source="gateway", code="capture_failed")
        self.budget.reserve(SCOPE, key, call.exchange_id, body)
        self.records.authorize(SCOPE, key, "pod", exchange_id=call.exchange_id)
        self.paid += 1
        if self.gate is not None and key.request_id == 0:
            assert self.gate.wait(2)
        if failure == "unknown":
            self.budget.settle(SCOPE, key, None)
            self.records.failed(SCOPE, key, "pod", "Interrupted", exchange_id=call.exchange_id)
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
        self.budget.settle(SCOPE, key, usage)
        self.records.issued(SCOPE, key, "pod", manifest, exchange_id=call.exchange_id)
        (self._state / call.exchange_id).mkdir(mode=0o700)
        result = CapturedResponse(call.exchange_id, status, response)
        self._save_response(result)
        if failure in ("delivery", "timeout"):
            raise WitnessError("Delivery unavailable", call.exchange_id, source="gateway",
                               code="capture_timeout" if failure == "timeout" else "delivery_failed")
        return result

    def export(self, output: Path, image: bytes = b"PNG", seed: int = 42) -> Path:
        execution = next(self._state.glob(f"executions/*/{Inputs.supplied(image, seed).id}/execution.json"))
        export_bundle(
            records=self.records,
            evidence=self.state,
            scope=SCOPE,
            execution=execution,
            output=output,
            proof_hash="c" * 64,
            pricing=self.pricing,
            verify=lambda _, path: json.loads((path / "manifest.json").read_bytes()),
        )
        return output


def test_concurrent_arrival_order_is_independent_of_identity_and_delivery(tmp_path: Path) -> None:
    live = Capture(tmp_path)
    live.gate = threading.Event()
    live.failures[1] = "failed"
    order = [0, 1]

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        def call(index: int) -> str:
            value = witness.create(model="fixture", input="image", request_id=index)
            if index == 1:
                assert live.gate is not None
                live.gate.set()
            return str(value.status) + value.output_text

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {i: pool.submit(call, i) for i in order}
            return jobs[0].result() + jobs[1].result()

    expected = run_generation(generate, b"PNG", 42, live, None)
    bundle = live.export(tmp_path / "bundle")
    order.reverse()
    assert run_replay(generate, b"PNG", 42, ReplayWitness(bundle, timeout=2), None) == expected == "completed0failed1"
    assert run_generation(generate, b"PNG", 42, live, None) == expected
    ordered = ReplayWitness(bundle, timeout=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ordered.create, model="fixture", input="image", request_id=0)
        with pytest.raises(FutureTimeout):
            first.result(timeout=0.05)
        second = pool.submit(ordered.create, model="fixture", input="image", request_id=1)
        assert second.result(timeout=2).status == "failed"
        assert first.result(timeout=2).status == "completed"
    ordered.finish(expected)
    assert live.paid == 2


@pytest.mark.parametrize("failure", ["before", "delivery", "timeout", "failed", "incomplete"])
def test_original_failures_replay_even_when_current_delivery_would_succeed(tmp_path: Path, failure: str) -> None:
    live = Capture(tmp_path)
    live.failures[0] = failure

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        try:
            value = witness.create(model="fixture", input="image")
            return str(value.status) + value.output_text
        except openai.APIConnectionError as error:
            assert isinstance(error.__cause__, WitnessError)
            return type(error).__name__ + str(error.__cause__.code)

    expected = run_generation(generate, b"PNG", 42, live, None)
    bundle = live.export(tmp_path / "bundle")
    assert run_replay(generate, b"PNG", 42, ReplayWitness(bundle), None) == expected
    assert run_generation(generate, b"PNG", 42, live, None) == expected
    assert live.paid == int(failure != "before")


@pytest.mark.parametrize("failure", ["provider", "unknown"])
def test_unknown_usage_is_reproduced_but_cannot_pass_prompt_budget(tmp_path: Path, failure: str) -> None:
    live = Capture(tmp_path)
    live.failures[0] = failure

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        try:
            return witness.create(model="fixture", input="image").output_text
        except openai.RateLimitError as error:
            return str(error.status_code) + str(error.code)
        except openai.APIConnectionError as error:
            assert isinstance(error.__cause__, WitnessError)
            return str(error.__cause__.code)

    expected = run_generation(generate, b"PNG", 42, live, None)
    replay = ReplayWitness(live.export(tmp_path / "bundle"))
    assert generate(b"PNG", 42, replay, None) == expected
    with pytest.raises(BudgetRejected, match="unknown usage"):
        replay.finish(expected)
    assert live.paid == 1


@pytest.mark.parametrize("limit,accepted", [(2, True), (1, False)])
def test_replay_charges_unique_consumed_calls_and_ignores_unrelated_capture(
    tmp_path: Path, limit: int, accepted: bool
) -> None:
    pricing = json.loads(PRICING)
    pricing["calls_per_prompt"] = limit
    live = Capture(tmp_path, pricing=canonical(pricing))
    live.create(model="fixture", input="unrelated", request_id=99)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        return "".join(witness.create(model="fixture", input="image", request_id=i).output_text for i in (0, 0, 1))

    expected = run_generation(generate, b"PNG", 42, live, None)
    bundle = live.export(tmp_path / "bundle")
    for _ in range(2):
        replay = ReplayWitness(bundle)
        assert generate(b"PNG", 42, replay, None) == expected
        if accepted:
            replay.finish(expected)
            assert len(replay.budget.charges) == 2
        else:
            with pytest.raises(BudgetRejected, match="exceeds"):
                replay.finish(expected)
    assert live.paid == 3


def test_replay_rejects_changed_missing_extra_inputs_and_tampering(tmp_path: Path) -> None:
    live = Capture(tmp_path)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        return witness.create(model="fixture", input="image").output_text

    run_generation(generate, b"PNG", 42, live, None)
    bundle = live.export(tmp_path / "bundle")
    with pytest.raises(ReplayMismatch):
        ReplayWitness(bundle).create(model="fixture", input="changed")
    with pytest.raises(ReplayMismatch):
        ReplayWitness(bundle).create(model="fixture", input="image", request_id=1)
    with pytest.raises(ReplayMismatch):
        ReplayWitness(bundle).finish("0")
    with pytest.raises(ReplayMismatch):
        run_replay(generate, b"changed", 42, ReplayWitness(bundle), None)
    replay = ReplayWitness(bundle)
    replay.create(model="fixture", input="image")
    with pytest.raises(ReplayMismatch):
        replay.create(model="fixture", input="image")
    replay = ReplayWitness(bundle)
    capture = next(iter(replay.bundle["captures"]))
    (bundle / capture / "response.sse").write_bytes(b"altered")
    with pytest.raises(ReplayMismatch):
        replay.create(model="fixture", input="image")


def test_stream_events_and_early_close_replay_with_typed_values(tmp_path: Path) -> None:
    live = Capture(tmp_path)

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        with witness.create(model="fixture", input="image", stream=True) as stream:
            event = next(stream)
            assert event.type == "response.completed"
            return event.response.output_text

    expected = run_generation(generate, b"PNG", 42, live, None)
    assert run_replay(generate, b"PNG", 42, ReplayWitness(live.export(tmp_path / "bundle")), None) == expected
    assert live.paid == 1


@pytest.mark.parametrize("limit,accepted", [("4", True), ("3.9", False)])
def test_each_prompt_is_charged_for_reused_calls_and_credit_limit_is_exact(
    tmp_path: Path, limit: str, accepted: bool
) -> None:
    pricing = json.loads(PRICING)
    pricing["credits_per_prompt"] = limit
    live = Capture(tmp_path, pricing=canonical(pricing))

    def generate(image: bytes, seed: int, witness: WitnessClient, renderer: None) -> str:
        return "".join(witness.create(model="fixture", input="shared", request_id=i).output_text for i in (0, 0, 1))

    for i, image in enumerate((b"first prompt", b"different prompt")):
        expected = run_generation(generate, image, 42, live, None)
        replay = ReplayWitness(live.export(tmp_path / str(i), image))
        assert generate(image, 42, replay, None) == expected
        if accepted:
            replay.finish(expected)
            assert len(replay.budget.charges) == 2
            assert sum(value for value in replay.budget.charges.values() if value is not None) == 4
        else:
            with pytest.raises(BudgetRejected, match="exceeds"):
                replay.finish(expected)
    assert live.paid == 2
