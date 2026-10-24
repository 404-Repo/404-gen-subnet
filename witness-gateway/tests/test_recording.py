from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Literal

import pytest
from support import MemoryState
from witness_client.calls import Generation, Invocation, RequestKey, canonical

from pod_gateway.budget import Budget, BudgetRejected, Charge
from pod_gateway.models import Scope
from pod_gateway.records import Call, CallRecords, RecordingConflict

SCOPE = Scope("competition", "test-0", "5" * 48)
BODY = canonical(
    {"model": "fixture", "input": "image", "stream": True, "service_tier": "default"}
)
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


def invocation(index: int = 0, *, request_id: int = 0, seed: int = 42) -> Invocation:
    return Invocation(
        generation=Generation.from_prompt(
            b"frozen PNG", seed, round_number="test-0"
        ),
        ordinal=index,
        request=RequestKey.from_body("api", BODY, request_id),
    )


def legacy_retry(
    state: MemoryState, scope: Scope, item: Invocation, body: bytes, *, authorized: bool = True,
    phase: Literal["reserved", "assigned", "capturing", "failed"] = "reserved",
) -> tuple[Call, Call]:
    records, budget = CallRecords(state), Budget(state, PRICING)
    original = records.reserve(scope, item)
    assigned = records.assign(scope, item, "00000000-0000-4000-8000-000000000001", "pod-one")
    assert records.claim(scope, item, "pod-one", assigned.capture_id or "")
    budget.reserve(scope, item, original.exchange_id, body)
    if authorized:
        records.authorize(scope, item, "pod-one", exchange_id=original.exchange_id)
    budget.settle(scope, item, None, exchange_id=original.exchange_id)
    records.failed(scope, item, "pod-one", "TimeoutError", exchange_id=original.exchange_id)
    recording = records.load(scope, item.generation.id)
    assert recording is not None
    original = recording.calls[item.request.id]
    replacement = original.model_copy(update={
        "exchange_id": str(uuid.uuid4()), "phase": phase, "authorized": False,
        "not_dispatched": phase == "failed",
        "start_id": None if phase == "reserved" else original.start_id,
        "pod_id": None if phase == "reserved" else original.pod_id,
        "capture_id": None if phase == "reserved" else str(uuid.uuid4()),
        "failure": "TimeoutError" if phase == "failed" else None,
    })
    recording.attempts[original.exchange_id] = original
    recording.calls[item.request.id] = replacement
    data = recording.model_dump()
    data["attempts"][original.exchange_id].pop("not_dispatched")
    if phase != "failed":
        data["calls"][item.request.id].pop("not_dispatched")
    state.put(records.key(scope, item.generation.id), canonical(data))
    state.put(records.index_key(scope, replacement.exchange_id), canonical({
        "generation_id": item.generation.id, "call_id": item.request.id,
    }))
    return original, replacement


def test_equal_requests_share_one_capture_but_keep_both_invocations() -> None:
    records = CallRecords(MemoryState())
    first = records.reserve(SCOPE, invocation())
    repeated = records.reserve(SCOPE, invocation(1))
    fresh = records.reserve(SCOPE, invocation(2, request_id=1))
    assert first.exchange_id == repeated.exchange_id != fresh.exchange_id
    recording = records.load(SCOPE, invocation().generation.id)
    assert (
        recording is not None
        and len(recording.entries) == 3
        and len(recording.calls) == 2
    )
    changed = invocation().model_copy(
        update={"request": RequestKey.from_body("api", BODY, 999)}
    )
    with pytest.raises(RecordingConflict):
        records.reserve(SCOPE, changed)


def test_expired_unstarted_reservation_can_resume_without_extending_an_active_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("pod_gateway.records.time.time", lambda: clock[0])
    records, item = CallRecords(MemoryState(), exchange_seconds=60), invocation()
    first = records.reserve(SCOPE, item)
    clock[0] += 61
    resumed = records.reserve(SCOPE, item)
    assert resumed.exchange_id == first.exchange_id and resumed.deadline > clock[0]
    assigned = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one")
    assert records.claim(SCOPE, item, "pod-one", assigned.capture_id or "")
    clock[0] += 61
    assert records.reserve(SCOPE, item).deadline == resumed.deadline
    recording = records.load(SCOPE, item.generation.id)
    assert recording is not None and len(recording.entries) == 1


def test_concurrent_retries_grant_exactly_one_capture_claim() -> None:
    records = CallRecords(MemoryState())
    with ThreadPoolExecutor(max_workers=8) as pool:
        calls = list(pool.map(lambda _: records.reserve(SCOPE, invocation()), range(8)))
    assert len({call.exchange_id for call in calls}) == 1
    assigned = records.assign(
        SCOPE, invocation(), "00000000-0000-4000-8000-000000000001", "pod-one"
    )
    assert assigned.capture_id is not None
    with ThreadPoolExecutor(max_workers=8) as pool:
        permissions = list(
            pool.map(
                lambda _: records.claim(
                    SCOPE, invocation(), "pod-one", assigned.capture_id or ""
                ),
                range(8),
            )
        )
    assert sum(permissions) == 1


def test_stored_result_delivery_order_is_durable_and_retries_do_not_repeat_it() -> None:
    state = MemoryState()
    records = CallRecords(state)
    first, second = invocation(), invocation(1, request_id=1)
    for item in (first, second):
        records.reserve(SCOPE, item)
        assigned = records.assign(
            SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one"
        )
        assert records.claim(SCOPE, item, "pod-one", assigned.capture_id or "")
        records.authorize(SCOPE, item, "pod-one", exchange_id=assigned.exchange_id)
        records.issued(SCOPE, item, "pod-one", b"sealed fixture manifest", exchange_id=assigned.exchange_id)
    assert records.deliver(SCOPE, second) == 0
    restarted = CallRecords(state)
    assert restarted.deliver(SCOPE, second) == 0
    assert restarted.deliver(SCOPE, first) == 1
    restarted.finish(SCOPE, first.generation, 2, "source")
    with pytest.raises(RecordingConflict):
        restarted.finish(SCOPE, first.generation, 2, "different source")
    with pytest.raises(RecordingConflict):
        restarted.reserve(SCOPE, invocation(2, request_id=2))


def test_generation_uses_frozen_prompt_and_seed_while_url_is_descriptive() -> (
    None
):
    original = Generation.from_prompt(
        b"PNG",
        42,
        round_number="test-0",
        prompt_url="https://example.org/one",
    )
    assert (
        original.id
        == Generation.from_prompt(
            b"PNG",
            42,
            round_number="test-0",
            prompt_url="https://example.org/two",
        ).id
    )
    assert (
        original.id
        != Generation.from_prompt(b"PNG", 43, round_number="test-0").id
    )
    assert (
        original.id
        != Generation.from_prompt(
            b"PNG changed", 42, round_number="test-0"
        ).id
    )
    assert (
        original.id
        != Generation.from_prompt(b"PNG", 42, round_number="test-1").id
    )


def test_usage_is_charged_once_and_seed_changes_do_not_reset_prompt_allowance() -> None:
    state = MemoryState()
    budget = Budget(state, PRICING)
    first = invocation()
    budget.reserve(SCOPE, first, "exchange-one", BODY)
    usage = {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10}
    budget.settle(SCOPE, first, usage)
    budget.settle(SCOPE, first, usage)
    assert Decimal(str(budget.summary(SCOPE, first)["known_credits"])) == Decimal("3.8")
    with pytest.raises(BudgetRejected):
        budget.reserve(SCOPE, first, "exchange-one", BODY)
    second = invocation(request_id=1, seed=43)
    budget.reserve(SCOPE, second, "exchange-two", BODY)
    assert budget.summary(SCOPE, first)["possible_dispatches"] == 2
    assert budget.summary(SCOPE, first)["remaining_credits"] is None
    with pytest.raises(BudgetRejected):
        budget.reserve(
            SCOPE, invocation(request_id=2, seed=44), "exchange-three", BODY
        )


def test_subscription_cached_tokens_use_the_full_input_rate() -> None:
    budget = Budget(MemoryState(), PRICING)
    item = invocation().model_copy(update={"request": RequestKey.from_body("codex", BODY)})
    budget.reserve(SCOPE, item, "subscription-exchange", BODY)
    budget.settle(SCOPE, item, {
        "input_tokens": 3, "output_tokens": 7, "total_tokens": 10,
        "input_tokens_details": {"cached_tokens": 3},
    })
    assert Decimal(str(budget.summary(SCOPE, item)["known_credits"])) == Decimal("3.8")
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"service_tier": None},
        {"service_tier": "auto"},
        {"service_tier": "fast"},
        {"service_tier": "ultrafast"},
        {"service_tier": "scale"},
        {"service_tier": "future-tier"},
    ],
)
def test_non_flex_tiers_use_frozen_model_rates(options: dict[str, object]) -> None:
    request = json.loads(BODY)
    del request["service_tier"]
    body = canonical({**request, **options})
    item = invocation().model_copy(update={"request": RequestKey.from_body("codex", body)})
    budget = Budget(MemoryState(), PRICING)
    budget.reserve(SCOPE, item, "subscription-exchange", body)
    budget.settle(SCOPE, item, {
        "input_tokens": 3, "output_tokens": 7, "total_tokens": 10,
        "input_tokens_details": {"cached_tokens": 3},
    })
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    assert Decimal(str(budget.summary(SCOPE, item)["known_credits"])) == Decimal("3.8")


def test_flex_is_rejected_before_reserving_budget() -> None:
    body = canonical({**json.loads(BODY), "service_tier": "flex"})
    item = invocation().model_copy(update={"request": RequestKey.from_body("codex", body)})
    budget = Budget(MemoryState(), PRICING)
    with pytest.raises(BudgetRejected, match="unsupported service tier"):
        budget.reserve(SCOPE, item, "subscription-exchange", body)
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 0


def test_verified_body_hash_is_checked_before_any_dispatch_reservation() -> None:
    budget = Budget(MemoryState(), PRICING)
    changed = json.loads(BODY)
    changed["input"] = "different image"
    with pytest.raises(BudgetRejected):
        budget.reserve(SCOPE, invocation(), "exchange-one", canonical(changed))
    assert budget.summary(SCOPE, invocation())["possible_dispatches"] == 0


def test_concurrent_retries_keep_completed_results_and_original_order() -> None:
    records = CallRecords(MemoryState())
    first, second = invocation(), invocation(1, request_id=1)
    for item in (first, second):
        records.reserve(SCOPE, item)
        call = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one")
        assert records.claim(SCOPE, item, "pod-one", call.capture_id or "")
        if item == first:
            records.authorize(SCOPE, item, "pod-one", exchange_id=call.exchange_id)
            records.issued(SCOPE, item, "pod-one", b"saved result", exchange_id=call.exchange_id)
            records.deliver(SCOPE, item)
        else:
            records.failed(SCOPE, item, "pod-one", "TimeoutError", exchange_id=call.exchange_id, not_dispatched=True)
    before = records.load(SCOPE, first.generation.id)
    assert before is not None
    failed = before.calls[second.request.id]
    with ThreadPoolExecutor(max_workers=8) as pool:
        retried = list(pool.map(lambda _: records.retry(SCOPE, second, failed.exchange_id), range(8)))
    assert len({call.exchange_id for call in retried}) == 1
    assert retried[0].exchange_id != failed.exchange_id
    after = records.load(SCOPE, first.generation.id)
    assert after is not None
    assert after.entries == before.entries
    assert after.calls[first.request.id] == before.calls[first.request.id]
    assert records.locate(SCOPE, failed.exchange_id) == (second, failed)
    assert records.locate(SCOPE, retried[0].exchange_id) == (second, retried[0])


def test_late_attempt_cannot_authorize_or_replace_its_retry() -> None:
    records, item = CallRecords(MemoryState()), invocation()
    original = records.reserve(SCOPE, item)
    call = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one")
    assert records.claim(SCOPE, item, "pod-one", call.capture_id or "")
    assert records.retry(SCOPE, item, original.exchange_id).phase == "capturing"
    records.failed(SCOPE, item, "pod-one", "TimeoutError", exchange_id=original.exchange_id, not_dispatched=True)
    retried = records.retry(SCOPE, item, original.exchange_id)
    assigned = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one")
    assert not records.claim(SCOPE, item, "pod-one", call.capture_id or "")
    assert records.claim(SCOPE, item, "pod-one", assigned.capture_id or "")
    with pytest.raises(RecordingConflict):
        records.authorize(SCOPE, item, "pod-one", exchange_id=original.exchange_id)
    with pytest.raises(RecordingConflict):
        records.issued(SCOPE, item, "pod-one", b"late result", exchange_id=original.exchange_id)
    records.failed(SCOPE, item, "pod-one", "TimeoutError", exchange_id=original.exchange_id)
    current = records.locate(SCOPE, retried.exchange_id)
    assert current is not None and current[1].phase == "capturing" and not current[1].authorized
    records.authorize(SCOPE, item, "pod-one", exchange_id=retried.exchange_id)
    records.issued(SCOPE, item, "pod-one", b"new result", exchange_id=retried.exchange_id)
    records.deliver(SCOPE, item)
    records.finish(SCOPE, item.generation, 1, "source")
    assert records.retry(SCOPE, item, original.exchange_id).phase == "issued"


def test_retry_cannot_replace_an_already_delivered_failure() -> None:
    records, item = CallRecords(MemoryState()), invocation()
    original = records.reserve(SCOPE, item)
    records.release(SCOPE, item)
    records.deliver(SCOPE, item)
    with pytest.raises(RecordingConflict, match="delivered"):
        records.retry(SCOPE, item, original.exchange_id)


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_failed_capture_requires_durable_non_dispatch_evidence(authorized: bool, legacy: bool) -> None:
    state, item = MemoryState(), invocation()
    records, budget = CallRecords(state), Budget(state, PRICING)
    original = records.reserve(SCOPE, item)
    assigned = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one")
    assert records.claim(SCOPE, item, "pod-one", assigned.capture_id or "")
    budget.reserve(SCOPE, item, original.exchange_id, BODY)
    if authorized:
        records.authorize(SCOPE, item, "pod-one", exchange_id=original.exchange_id)
    budget.settle(SCOPE, item, None, exchange_id=original.exchange_id)
    records.failed(SCOPE, item, "pod-one", "TimeoutError", exchange_id=original.exchange_id)
    if legacy:
        key = records.key(SCOPE, item.generation.id)
        recording = json.loads(state.read(key) or b"{}")
        recording["calls"][item.request.id].pop("not_dispatched")
        state.put(key, canonical(recording))
    before = records.load(SCOPE, item.generation.id)
    assert before is not None
    retried = records.retry(SCOPE, item, original.exchange_id)
    assert retried.exchange_id == original.exchange_id and retried.phase == "failed"
    after = records.load(SCOPE, item.generation.id)
    assert after is not None and after.calls == before.calls and after.attempts == before.attempts
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    assert budget.summary(SCOPE, item)["pending_usage"] == 1
    assert budget.summary(SCOPE, item)["remaining_credits"] is None


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("phase", ["reserved", "assigned", "capturing", "failed"])
def test_legacy_replacement_recovers_the_original_and_cannot_dispatch(
    authorized: bool, phase: Literal["reserved", "assigned", "capturing", "failed"],
) -> None:
    state, item = MemoryState(), invocation()
    original, replacement = legacy_retry(state, SCOPE, item, BODY, authorized=authorized, phase=phase)
    records, budget = CallRecords(state), Budget(state, PRICING)
    before = records.load(SCOPE, item.generation.id)
    assert records.reserve(SCOPE, item) == original
    assert records.retry(SCOPE, item, replacement.exchange_id) == original
    assert records.assign(SCOPE, item, original.start_id or "", "pod-one") == original
    assert not records.claim(SCOPE, item, "pod-one", replacement.capture_id or "")
    with pytest.raises(RecordingConflict):
        records.authorize(SCOPE, item, "pod-one", exchange_id=replacement.exchange_id)
    with pytest.raises(BudgetRejected):
        budget.reserve(SCOPE, item, replacement.exchange_id, BODY)
    after = records.load(SCOPE, item.generation.id)
    assert before is not None and after is not None
    assert after.calls == before.calls and after.attempts == before.attempts
    summary = budget.summary(SCOPE, item)
    assert summary["possible_dispatches"] == summary["pending_usage"] == 1
    assert summary["remaining_credits"] is None


def test_legacy_authorized_retries_retain_unknown_usage_and_count_both_dispatches() -> None:
    state, item = MemoryState(), invocation()
    budget = Budget(state, PRICING)
    budget.reserve(SCOPE, item, "first-attempt", BODY)
    budget.settle(SCOPE, item, None, exchange_id="first-attempt")
    key = budget.key(SCOPE, item)
    ledger = json.loads(state.read(key) or b"{}")
    ledger["charges"][f"{budget.charge_key(item)}/second-attempt"] = Charge(
        exchange_id="second-attempt", model="fixture"
    ).model_dump()
    state.put(key, canonical(ledger))
    budget.settle(SCOPE, item, {"input_tokens": 3, "output_tokens": 7}, exchange_id="second-attempt")
    budget.settle(SCOPE, item, None, exchange_id="first-attempt")
    summary = budget.summary(SCOPE, item)
    assert summary["possible_dispatches"] == 2 and summary["pending_usage"] == 1
    assert Decimal(str(summary["known_credits"])) == Decimal("3.8")
    assert summary["remaining_credits"] is None
    with pytest.raises(BudgetRejected):
        budget.reserve(SCOPE, item, "second-attempt", BODY)
    with pytest.raises(BudgetRejected):
        budget.reserve(SCOPE, item, "third-attempt", BODY)


def test_same_request_has_independent_exchanges_and_usage_for_each_prompt() -> None:
    state = MemoryState()
    records, budget = CallRecords(state), Budget(state, PRICING)
    first = invocation()
    second = first.model_copy(update={
        "generation": Generation.from_prompt(b"another PNG", 42, round_number="test-0"),
    })
    first_call = records.reserve(SCOPE, first)
    second_call = records.reserve(SCOPE, second)
    assert first_call.exchange_id != second_call.exchange_id
    for item, call in ((first, first_call), (second, second_call)):
        budget.reserve(SCOPE, item, call.exchange_id, BODY)
        budget.settle(SCOPE, item, {"input_tokens": 3, "output_tokens": 7})
    assert budget.summary(SCOPE, first)["possible_dispatches"] == 1
    assert budget.summary(SCOPE, second)["possible_dispatches"] == 1
    assert budget.summary(SCOPE, first)["known_credits"] == "3.8"
    assert budget.summary(SCOPE, second)["known_credits"] == "3.8"
