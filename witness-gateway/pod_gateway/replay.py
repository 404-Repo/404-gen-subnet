"""Export an offline replay bundle only after rechecking captured TLS evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from miner_runtime.observations import Execution, Schedule
from witness_client.calls import request_digest

from .models import Scope, json_bytes
from .records import CallRecords
from .recovery import response_failure
from .server import Evidence

Verify = Callable[[str, Path], dict[str, object]]


def native_verifier(script: Path, parameters: Path) -> Verify:
    def verify(provider: str, directory: Path) -> dict[str, object]:
        result = subprocess.run(
            ["node", str(script), "replay", str(directory), str(parameters), provider],
            capture_output=True,
            check=False,
            timeout=180,
            env={**os.environ, "NODE_OPTIONS": "--max-old-space-size=384"},
        )
        if result.returncode:
            raise ValueError("Captured TLS evidence did not verify")
        value: object = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("Invalid verifier reply")  # noqa: TRY004 - invalid subprocess data
        return value

    return verify


def export_bundle(
    *,
    records: CallRecords,
    evidence: Evidence,
    scope: Scope,
    execution: Path,
    output: Path,
    proof_hash: str,
    pricing: bytes,
    verify: Verify,
) -> None:
    """Combine trusted host observations with independently verified provider evidence."""
    raw = execution.read_bytes()
    observed = Execution.model_validate_json(raw)
    Schedule(observed)
    if not observed.finished or any(entry.exited is None for entry in observed.observations):
        raise ValueError("Execution observations are incomplete")
    if output.exists():
        raise ValueError("Replay output already exists")
    pricing_hash = hashlib.sha256(pricing).hexdigest()
    temporary = Path(tempfile.mkdtemp(prefix=".witness-replay-", dir=output.parent))
    captures: dict[str, object] = {}
    try:
        for entry in observed.observations:
            if entry.action != "create" or entry.exchange_id is None or entry.exchange_id in captures:
                continue
            located = records.locate(scope, entry.exchange_id)
            if located is None or located[0] != entry.request:
                raise ValueError("Execution references an unknown captured call")
            key, call = located
            directory = temporary / call.exchange_id
            directory.mkdir(mode=0o700)
            prefix = f"rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
            for name in (
                "client.bin",
                "server.bin",
                "authorization-server.bin",
                "claim.json",
                "manifest.json",
                "request.json",
                "response.sse",
                "session.json",
            ):
                content = evidence.read(prefix + name)
                if content is not None:
                    (directory / name).write_bytes(content)
            model, usage = "", None
            if call.manifest_sha256 is not None:
                manifest_bytes = (directory / "manifest.json").read_bytes()
                manifest = json.loads(manifest_bytes)
                request, response = (directory / "request.json").read_bytes(), (directory / "response.sse").read_bytes()
                binding = {
                    "call_id": key.id,
                    "request_key": key.model_dump(),
                    "hotkey": scope.hotkey,
                    "round": scope.round,
                    "exchange_id": call.exchange_id,
                    "proof_key_sha256": proof_hash,
                    "pricing_sha256": pricing_hash,
                }
                if (
                    hashlib.sha256(manifest_bytes).hexdigest() != call.manifest_sha256
                    or any(manifest.get(k) != value for k, value in binding.items())
                    or request_digest(request) != key.request_sha256
                    or manifest.get("request_sha256") != hashlib.sha256(request).hexdigest()
                    or manifest.get("response_sha256") != hashlib.sha256(response).hexdigest()
                ):
                    raise ValueError("Recorded evidence binding differs")
                verified = verify(key.provider, directory)
                if (
                    any(
                        verified.get(k) != manifest.get(k)
                        for k in ("request_sha256", "response_sha256", "provider_status", "usage")
                    )
                    or verified.get("session_id") != key.session_id
                    or (directory / "request.json").read_bytes() != request
                    or (directory / "response.sse").read_bytes() != response
                ):
                    raise ValueError("TLS verification differs from retained outcome")
                model, usage = json.loads(request)["model"], verified.get("usage")
                rejected = response_failure(manifest, response)
                if rejected is not None:
                    call = call.model_copy(update={"phase": "failed", "failure": rejected})
            elif entry.failure is None:
                raise ValueError("Successful observation has no issued capture")
            captures[call.exchange_id] = {
                "call": call.model_dump(exclude={"capture_id"}),
                "model": model,
                "usage": usage,
            }
        (temporary / "execution.json").write_bytes(raw)
        (temporary / "pricing.json").write_bytes(pricing)
        (temporary / "verified.json").write_bytes(
            json_bytes(
                {
                    "version": 2,
                    "execution_sha256": hashlib.sha256(raw).hexdigest(),
                    "scope_key": scope.key,
                    "proof_key_sha256": proof_hash,
                    "pricing_sha256": pricing_hash,
                    "captures": captures,
                }
            )
        )
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
