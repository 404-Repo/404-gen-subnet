"""Real R2 conditional writes with synthetic state and injected acknowledgement loss."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

logging.getLogger("urllib3").setLevel(logging.CRITICAL)
SOURCE = Path(__file__).resolve().parents[2]
from bittensor_wallet import Keypair
from witness_client.calls import RequestKey, canonical

from pod_gateway.budget import Budget
from pod_gateway.models import Scope
from pod_gateway.records import CallRecords
from pod_gateway.state import R2StateStore, StateConflict, WriteUncertain


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Private local JSON report and object index")
    args = parser.parse_args()
    e = os.environ
    client = boto3.client(
        "s3",
        endpoint_url=e["R2_ENDPOINT"],
        aws_access_key_id=e["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}, max_pool_connections=32),
    )
    prefix = "reliability-tests/r2-recovery-" + uuid.uuid4().hex + "/"
    cases: list[dict[str, object]] = []
    report: dict[str, object] = {
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip(),
        "prefix": prefix,
        "bucket": e["R2_BUCKET"],
        "cases": cases,
    }

    class Faults:
        mode = "normal"

        def get_object(self, **kw: object) -> dict[str, object]:
            if self.mode == "lost-ack-no-read":
                raise ClientError(
                    {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject"
                )
            return cast(dict[str, object], client.get_object(**{**kw, "Key": prefix + str(kw["Key"])}))

        def put_object(self, **kw: object) -> dict[str, object]:
            if self.mode == "lost-before":
                raise ReadTimeoutError(endpoint_url="https://synthetic.invalid")
            result = client.put_object(**{**kw, "Key": prefix + str(kw["Key"])})
            if self.mode in ("lost-after", "lost-ack-no-read"):
                raise ReadTimeoutError(endpoint_url="https://synthetic.invalid")
            return cast(dict[str, object], result)

    fault = Faults()
    state = R2StateStore(fault, e["R2_BUCKET"])
    first = state.save("cas.json", b'{"revision":"one"}', None)
    try:
        state.save("cas.json", b'{"revision":"forbidden"}', None)
    except StateConflict:
        pass
    else:
        raise AssertionError("Concurrent create was not rejected")
    second = state.save("cas.json", b'{"revision":"two"}', first.etag)
    try:
        state.save("cas.json", b'{"revision":"stale"}', first.etag)
    except StateConflict:
        pass
    else:
        raise AssertionError("Stale update was not rejected")
    loaded = state.load("cas.json")
    assert loaded is not None and loaded.data == second.data
    cases.append({"case": "real-r2-cas", "outcome": "conditional create and stale update rejected"})
    for mode in ("lost-before", "lost-after", "lost-ack-no-read"):
        fault.mode = mode
        try:
            state.save(mode + ".json", canonical({"revision": str(uuid.uuid4())}), None)
        except WriteUncertain:
            assert mode != "lost-after"
            outcome = "uncertain; provider release prohibited"
        else:
            assert mode == "lost-after"
            outcome = "exact readback confirms durable write"
        finally:
            fault.mode = "normal"
        cases.append({"case": mode, "outcome": outcome, "durable": state.load(mode + ".json") is not None})

    scope = Scope(
        "reliability-r2",
        "r2-fault-" + uuid.uuid4().hex[:10],
        Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address,
    )
    body = canonical(
        {
            "model": "fixture",
            "input": "Synthetic storage fault fixture",
            "stream": True,
            "tools": [],
            "service_tier": "default",
        }
    )
    key = RequestKey.from_body("api", body)
    records = CallRecords(state)
    pricing = canonical(
        {
            "models": {"fixture": {"input_credits_per_token": 0.1, "output_credits_per_token": 0.2}},
            "long_context_above_input_tokens": 100000,
        }
    )
    budget = Budget(state, pricing)
    start = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=16) as pool:
        assigned = list(
            pool.map(lambda _: records.assign(scope, key, start, "temporary-pod", allow_reassign=True), range(16))
        )
    assert len({call.exchange_id for call in assigned}) == 1
    with ThreadPoolExecutor(max_workers=16) as pool:
        claimed = list(
            pool.map(lambda call: records.claim(scope, key, "temporary-pod", call.capture_id or ""), assigned)
        )
    assert sum(claimed) == 1
    call = assigned[0]
    budget.reserve(scope, key, call.exchange_id, body)
    records.authorize(scope, key, "temporary-pod", exchange_id=call.exchange_id)
    records.failed(scope, key, "temporary-pod", "SyntheticConnectionLost", exchange_id=call.exchange_id)
    budget.settle(scope, key, None)
    assert records.retry(scope, key, call.exchange_id).exchange_id == call.exchange_id
    assert budget.summary(scope, key)["pending_usage"] == 1
    cases.append(
        {
            "case": "16-real-r2-claims",
            "captures_admitted": sum(claimed),
            "possible_dispatches": 1,
            "usage": "unknown",
            "replacement_allowed": False,
        }
    )
    key = RequestKey.from_body("api", body, request_id=1)
    call = records.assign(scope, key, start, "temporary-pod")
    assert records.claim(scope, key, "temporary-pod", call.capture_id or "")
    records.failed(
        scope, key, "temporary-pod", "SyntheticSetupFailure", exchange_id=call.exchange_id, not_dispatched=True
    )
    with ThreadPoolExecutor(max_workers=16) as pool:
        replacements = list(
            pool.map(
                lambda _: records.assign(scope, key, start, "temporary-pod", expected_exchange_id=call.exchange_id),
                range(16),
            )
        )
    next_call = replacements[0]
    assert next_call.exchange_id != call.exchange_id and len({c.exchange_id for c in replacements}) == 1
    assert records.claim(scope, key, "temporary-pod", next_call.capture_id or "")
    records.failed(
        scope, key, "temporary-pod", "SyntheticSetupFailure", exchange_id=next_call.exchange_id, not_dispatched=True
    )
    assert (
        records.assign(scope, key, start, "temporary-pod", expected_exchange_id=call.exchange_id).exchange_id
        == next_call.exchange_id
    )
    cases.append(
        {"case": "16-safe-retries-and-stale-registration", "new_attempts": 1, "stale_attempt_replaced_newer": False}
    )
    objects: list[dict[str, object]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=e["R2_BUCKET"], Prefix=prefix):
        for item in page.get("Contents", []):
            objects.append({"key": item["Key"], "size": item["Size"]})
    report["objects"] = objects
    report["source_sha256"] = {
        name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
        for name in (
            "witness-gateway/pod_gateway/state.py",
            "witness-gateway/pod_gateway/records.py",
            "witness-gateway/pod_gateway/budget.py",
        )
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"prefix": prefix, "cases": report["cases"], "objects": len(objects)}))


if __name__ == "__main__":
    main()
