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

from witness_client.calls import request_digest
from witness_client.pods import RecordingView

from .models import Scope, json_bytes
from .records import CallRecords
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
    generation_id: str,
    output: Path,
    proof_hash: str,
    pricing_hash: str,
    verify: Verify,
) -> None:
    recording = records.load(scope, generation_id)
    if recording is None or not recording.finished:
        raise ValueError("Generation has no complete recording")
    if output.exists():
        raise ValueError("Replay output already exists")
    data = recording.model_dump(exclude={"attempts"})
    data["calls"] = {
        key: call.model_dump(exclude={"capture_id"})
        for key, call in recording.calls.items()
    }
    public = RecordingView.model_validate(data)
    providers = {call.request.provider for call in public.calls.values()}
    if len(providers) > 1 or any(
        call.phase != "issued" for call in public.calls.values()
    ):
        raise ValueError("Recording contains unresolved calls or mixed providers")
    if sorted(e.delivery for e in public.entries if e.delivery is not None) != list(
        range(len(public.entries))
    ):
        raise ValueError("Recording has incomplete delivery order")
    temporary = Path(tempfile.mkdtemp(prefix=".witness-replay-", dir=output.parent))
    try:
        for call_id, call in public.calls.items():
            if call_id != call.request.id:
                raise ValueError("Recording request identity differs")
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
                if content is None:
                    raise ValueError("Captured evidence is missing")
                (directory / name).write_bytes(content)
            manifest_bytes = (directory / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            request, response = (
                (directory / "request.json").read_bytes(),
                (directory / "response.sse").read_bytes(),
            )
            if (
                hashlib.sha256(manifest_bytes).hexdigest() != call.manifest_sha256
                or manifest.get("generation_id") != generation_id
                or manifest.get("call_id") != call_id
                or manifest.get("request_key") != call.request.model_dump()
                or manifest.get("hotkey") != scope.hotkey
                or manifest.get("round") != scope.round
                or manifest.get("exchange_id") != call.exchange_id
                or manifest.get("proof_key_sha256") != proof_hash
                or manifest.get("pricing_sha256") != pricing_hash
                or request_digest(request) != call.request.request_sha256
                or manifest.get("request_sha256") != hashlib.sha256(request).hexdigest()
                or manifest.get("response_sha256")
                != hashlib.sha256(response).hexdigest()
            ):
                raise ValueError("Recorded evidence binding differs")
            verified = verify(call.request.provider, directory)
            if (
                verified.get("request_sha256") != manifest["request_sha256"]
                or verified.get("response_sha256") != manifest["response_sha256"]
                or verified.get("provider_status") != manifest["provider_status"]
                or verified.get("usage") != manifest.get("usage")
                or (directory / "request.json").read_bytes() != request
                or (directory / "response.sse").read_bytes() != response
            ):
                raise ValueError("TLS verification differs from retained response")
        public_bytes = public.model_dump_json().encode()
        (temporary / "recording.json").write_bytes(public_bytes)
        (temporary / "verified.json").write_bytes(
            json_bytes(
                {
                    "version": 1,
                    "recording_sha256": hashlib.sha256(public_bytes).hexdigest(),
                    "hotkey": scope.hotkey,
                    "provider": next(iter(providers), "api"),
                    "proof_key_sha256": proof_hash,
                    "pricing_sha256": pricing_hash,
                }
            )
        )
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
