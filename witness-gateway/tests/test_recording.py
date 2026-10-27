from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from support import MemoryState
from witness_client.calls import RequestKey, canonical, request_digest

from pod_gateway.budget import Budget, BudgetRejected
from pod_gateway.models import Scope
from pod_gateway.records import Call, CallRecords, RecordingConflict

SCOPE = Scope("competition", "test-0", "5" * 48)
BODY = canonical({"model": "fixture", "input": "image", "stream": True, "service_tier": "default"})
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


def invocation(index: int = 0, *, request_id: int = 0, seed: int = 42) -> RequestKey:
    return RequestKey.from_body("api", BODY, request_id)


def test_equal_calls_reuse_captures_independently_of_arrival_order() -> None:
    records = CallRecords(MemoryState())
    first = records.reserve(SCOPE, invocation())
    assert records.reserve(SCOPE, invocation(99, seed=17)).exchange_id == first.exchange_id
    fresh = records.reserve(SCOPE, invocation(request_id=1))
    assert fresh.exchange_id != first.exchange_id
    assert records.reserve(SCOPE, invocation()).exchange_id == first.exchange_id


@pytest.mark.parametrize(
    "session_id,call_id",
    [
        (None, "99d30116834befb768f9b4a51af7fc7e8378aa0650396098027e556fefeef6f8"),
        ("fixture-session", "44b0b951058a9133cfd23e907f8ffd8db661106893ea5f1cd7fcc54ec21992f4"),
    ],
)
def test_request_id_wire_key_round_trips_through_call_records(session_id: str | None, call_id: str) -> None:
    wire: dict[str, object] = {"provider": "codex", "request_sha256": request_digest(BODY), "request_id": 7}
    if session_id is not None:
        wire["session_id"] = session_id
    item = RequestKey.model_validate_json(canonical(wire))
    assert item == RequestKey.from_body("codex", BODY, request_id=7, session_id=session_id)
    assert item.id == call_id
    state = MemoryState()
    call = CallRecords(state).reserve(SCOPE, item)
    saved = CallRecords(state).load(SCOPE, call_id)
    assert saved is not None and saved.call.exchange_id == call.exchange_id
    assert saved.call.request.model_dump() == wire


def test_live_usage_has_no_per_prompt_admission_limit() -> None:
    budget = Budget(MemoryState(), PRICING)
    for i in range(4):
        item = invocation(request_id=i)
        budget.reserve(SCOPE, item, str(i), BODY)
        budget.settle(SCOPE, item, {"input_tokens": 3, "output_tokens": 7})
        assert budget.summary(SCOPE, item)["known_credits"] == "3.8"
    with pytest.raises(BudgetRejected):
        budget.reserve(SCOPE, invocation(), "another-attempt", BODY)


@pytest.mark.parametrize("authorized", [False, True])
def test_unknown_dispatch_cannot_retry_or_erase_unknown_usage(authorized: bool) -> None:
    state = MemoryState()
    records, budget, item = CallRecords(state), Budget(state, PRICING), invocation()
    call = records.reserve(SCOPE, item)
    assigned = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod")
    assert records.claim(SCOPE, item, "pod", assigned.capture_id or "")
    budget.reserve(SCOPE, item, call.exchange_id, BODY)
    if authorized:
        records.authorize(SCOPE, item, "pod", exchange_id=call.exchange_id)
    budget.settle(SCOPE, item, None)
    records.failed(SCOPE, item, "pod", "Interrupted", exchange_id=call.exchange_id)
    assert records.assign(
        SCOPE, item, assigned.start_id or "", "pod", expected_exchange_id=call.exchange_id,
    ).exchange_id == call.exchange_id
    assert budget.summary(SCOPE, item)["pending_usage"] == 1


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
    recording = records.load(SCOPE, item.id)
    assert recording is not None and recording.call.exchange_id == first.exchange_id


def test_expired_unclaimed_assignment_can_resume_without_extending_a_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("pod_gateway.records.time.time", lambda: clock[0])
    records, item = CallRecords(MemoryState(), exchange_seconds=60), invocation()
    start_id = "00000000-0000-4000-8000-000000000001"
    first = records.assign(SCOPE, item, start_id, "pod-one", allow_reassign=True)
    clock[0] += 61
    resumed = records.assign(SCOPE, item, start_id, "pod-one", allow_reassign=True)
    assert resumed.exchange_id == first.exchange_id and resumed.deadline > clock[0]
    assert not records.claim(SCOPE, item, "pod-one", first.capture_id or "")
    assert records.claim(SCOPE, item, "pod-one", resumed.capture_id or "")
    clock[0] += 61
    active = records.assign(SCOPE, item, start_id, "pod-one", allow_reassign=True)
    assert active.phase == "capturing" and active.deadline == resumed.deadline


def test_concurrent_retries_grant_exactly_one_capture_claim() -> None:
    records = CallRecords(MemoryState())
    with ThreadPoolExecutor(max_workers=8) as pool:
        calls = list(pool.map(lambda _: records.assign(
            SCOPE, invocation(), "00000000-0000-4000-8000-000000000001", "pod-one", allow_reassign=True,
        ), range(8)))
    assert len({call.exchange_id for call in calls}) == 1
    assert len({call.capture_id for call in calls}) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        permissions = list(
            pool.map(
                lambda call: records.claim(SCOPE, invocation(), "pod-one", call.capture_id or ""),
                calls,
            )
        )
    assert sum(permissions) == 1


def test_subscription_cached_tokens_use_the_full_input_rate() -> None:
    budget = Budget(MemoryState(), PRICING)
    item = RequestKey.from_body("codex", BODY)
    budget.reserve(SCOPE, item, "subscription-exchange", BODY)
    budget.settle(
        SCOPE,
        item,
        {
            "input_tokens": 3,
            "output_tokens": 7,
            "total_tokens": 10,
            "input_tokens_details": {"cached_tokens": 3},
        },
    )
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
    item = RequestKey.from_body("codex", body)
    budget = Budget(MemoryState(), PRICING)
    budget.reserve(SCOPE, item, "subscription-exchange", body)
    budget.settle(
        SCOPE,
        item,
        {
            "input_tokens": 3,
            "output_tokens": 7,
            "total_tokens": 10,
            "input_tokens_details": {"cached_tokens": 3},
        },
    )
    assert budget.summary(SCOPE, item)["possible_dispatches"] == 1
    assert Decimal(str(budget.summary(SCOPE, item)["known_credits"])) == Decimal("3.8")


def test_flex_is_rejected_before_reserving_budget() -> None:
    body = canonical({**json.loads(BODY), "service_tier": "flex"})
    item = RequestKey.from_body("codex", body)
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


def test_late_attempt_cannot_authorize_or_replace_its_retry() -> None:
    records, item = CallRecords(MemoryState()), invocation()
    original = records.reserve(SCOPE, item)
    call = records.assign(SCOPE, item, "00000000-0000-4000-8000-000000000001", "pod-one")
    assert records.claim(SCOPE, item, "pod-one", call.capture_id or "")
    assert records.assign(
        SCOPE, item, call.start_id or "", "pod-one", expected_exchange_id=original.exchange_id,
    ).phase == "capturing"
    records.failed(SCOPE, item, "pod-one", "TimeoutError", exchange_id=original.exchange_id, not_dispatched=True)
    retried = assigned = records.assign(
        SCOPE, item, call.start_id or "", "pod-one", expected_exchange_id=original.exchange_id,
    )
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
    assert records.assign(
        SCOPE, item, call.start_id or "", "pod-one", expected_exchange_id=original.exchange_id,
    ).phase == "issued"


def test_concurrent_and_stale_setup_recovery_cannot_advance_a_newer_attempt() -> None:
    records, item = CallRecords(MemoryState()), invocation()
    start_id = "00000000-0000-4000-8000-000000000001"
    first = records.assign(SCOPE, item, start_id, "pod-one")
    assert records.claim(SCOPE, item, "pod-one", first.capture_id or "")
    records.failed(SCOPE, item, "pod-one", "SetupFailed", exchange_id=first.exchange_id, not_dispatched=True)

    def recover(exchange_id: str) -> Call:
        return records.assign(SCOPE, item, start_id, "pod-one", expected_exchange_id=exchange_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        replacements = list(pool.map(lambda _: recover(first.exchange_id), range(8)))
    second = replacements[0]
    assert second.exchange_id != first.exchange_id and second.phase == "assigned"
    assert all(call == second for call in replacements)
    assert records.claim(SCOPE, item, "pod-one", second.capture_id or "")
    records.failed(SCOPE, item, "pod-one", "SetupFailed", exchange_id=second.exchange_id, not_dispatched=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        stale = list(pool.map(lambda _: recover(first.exchange_id), range(8)))
    assert all(call.exchange_id == second.exchange_id and call.phase == "failed" for call in stale)
    third = recover(second.exchange_id)
    assert third.exchange_id != second.exchange_id
    for previous in (first, second):
        located = records.locate(SCOPE, previous.exchange_id)
        assert located is not None and located[1].not_dispatched
