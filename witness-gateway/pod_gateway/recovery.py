"""Finish a captured inference from durable evidence without its original worker."""

import hashlib
import json
from collections.abc import Callable

from witness_client.calls import RequestKey, request_digest
from witness_client.response import completed_response

from .budget import Budget
from .models import Scope, object_dict
from .records import Call, CallRecords, RecordingConflict


def response_failure(metadata: dict[str, object], response: bytes) -> str | None:
    if metadata.get("provider_status") != 200:
        return "ProviderHTTPError"
    if metadata.get("usage") is None:
        return "InvalidProviderResponse"
    try:
        completed_response(response, raise_failed=False)
    except Exception:  # noqa: BLE001 - unusable provider output is retained as a failed attempt
        return "InvalidProviderResponse"
    return None


def recover_issued(
    records: CallRecords,
    budget: Budget,
    read: Callable[[str], bytes | None],
    scope: Scope,
    invocation: RequestKey,
    call: Call,
    proof_hash: str,
    save_failure: Callable[[Call], None] | None = None,
) -> Call:
    if call.phase != "capturing" or not call.authorized:
        return call
    prefix = f"rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
    manifest = read(prefix + "manifest.json")
    if manifest is None:
        return call
    metadata = object_dict(json.loads(manifest))
    binding = {
        "call_id": invocation.id,
        "request_key": invocation.model_dump(),
        "round": scope.round,
        "hotkey": scope.hotkey,
        "exchange_id": call.exchange_id,
        "pod_id": call.pod_id,
        "pod_start_id": call.start_id,
        "proof_key_sha256": proof_hash,
        "pricing_sha256": budget.pricing_hash,
    }
    if any(metadata.get(key) != value for key, value in binding.items()):
        raise RecordingConflict("Durable capture binding differs")
    request, response = read(prefix + "request.json"), read(prefix + "response.sse")
    if request is None or response is None:
        return call
    if (
        request_digest(request) != invocation.request_sha256
        or metadata.get("request_sha256") != hashlib.sha256(request).hexdigest()
        or metadata.get("response_sha256") != hashlib.sha256(response).hexdigest()
        or type(metadata.get("provider_status")) is not int
    ):
        raise RecordingConflict("Durable capture payload differs")
    for name in ("client.bin", "server.bin", "authorization-server.bin", "claim.json", "session.json"):
        if read(prefix + name) is None:
            return call
    # Recovery finishes the saved outcome without another provider request.
    failure = response_failure(metadata, response)
    assert call.pod_id is not None
    if failure is None:
        budget.settle(scope, invocation, metadata.get("usage"), exchange_id=call.exchange_id)
        records.issued(scope, invocation, call.pod_id, manifest, exchange_id=call.exchange_id)
    else:
        failed = call.model_copy(update={
            "phase": "failed", "failure": failure, "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        })
        budget.discard(scope, invocation, call.exchange_id)
        records.failed(scope, invocation, call.pod_id, failure, exchange_id=call.exchange_id, manifest=manifest)
        if save_failure is not None:
            save_failure(failed)
    return call.model_copy(update={
        "phase": "issued" if failure is None else "failed",
        "failure": failure,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    })
