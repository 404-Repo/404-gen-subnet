"""Finish a captured inference from durable evidence without its original worker."""

import hashlib
import json
from collections.abc import Callable

from witness_client.calls import Invocation, request_digest

from .budget import Budget
from .models import Scope, object_dict
from .records import Call, CallRecords, RecordingConflict


def recover_issued(
    records: CallRecords,
    budget: Budget,
    read: Callable[[str], bytes | None],
    scope: Scope,
    invocation: Invocation,
    call: Call,
    proof_hash: str,
) -> Call:
    if call.phase != "capturing" or not call.authorized:
        return call
    prefix = f"rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
    manifest = read(prefix + "manifest.json")
    if manifest is None:
        return call
    metadata = object_dict(json.loads(manifest))
    binding = {
        "generation_id": invocation.generation.id,
        "call_id": invocation.request.id,
        "request_key": invocation.request.model_dump(),
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
        request_digest(request) != invocation.request.request_sha256
        or metadata.get("request_sha256") != hashlib.sha256(request).hexdigest()
        or metadata.get("response_sha256") != hashlib.sha256(response).hexdigest()
        or type(metadata.get("provider_status")) is not int
    ):
        raise RecordingConflict("Durable capture payload differs")
    for name in ("client.bin", "server.bin", "authorization-server.bin", "claim.json", "session.json"):
        if read(prefix + name) is None:
            return call
    # Both writes are idempotent. A restart between them repeats settlement, never
    # provider inference, and issued still means accounting and evidence are durable.
    budget.settle(scope, invocation, metadata.get("usage"))
    assert call.pod_id is not None
    records.issued(scope, invocation, call.pod_id, manifest)
    return call.model_copy(update={"phase": "issued", "manifest_sha256": hashlib.sha256(manifest).hexdigest()})
