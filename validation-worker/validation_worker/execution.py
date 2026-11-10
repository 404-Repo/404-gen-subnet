import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pydantic import BaseModel
from renderer_client import RenderRejected
from renderer_client.client import PNG_SIGNATURE
from subnet_common.competition.validation import ValidationResult, ValidationVerdict
from subnet_common.r2_client import R2Client
from subnet_common.witness.models import Scope, json_bytes, object_dict
from subnet_common.witness.pricing import BudgetRejected, PromptBudget
from subnet_common.witness.rounds import PromptImage
from witness_client.calls import RequestKey, request_digest
from witness_pod import PROOF_PARAMETERS, VERIFY_SCRIPT
from witness_pod.records import CallRecord, CallRecords

from validation_worker.inputs import ValidationInputs
from validation_worker.preparation import optional_object, read_object
from validation_worker.sandbox import MinerResult, ModelCall, RenderCall, Sandbox, encoded
from validation_worker.settings import Settings


class ValidationFailed(ValueError):
    pass


class VerifiedCapture(BaseModel):
    request_sha256: str
    response_sha256: str
    session_id: str | None
    provider_status: int
    usage: dict[str, int] | None


async def model_response(
    query: ModelCall,
    inputs: ValidationInputs,
    r2: R2Client,
    settings: Settings,
    directory: Path,
    pricing: bytes,
    budget: PromptBudget,
) -> tuple[str, int, bytes]:
    body = base64.b64decode(query.body, validate=True)
    request = RequestKey.from_body(settings.witness_provider, body, query.request_id, session_id=query.session_id)
    scope = Scope(settings.witness_deployment, inputs.submission.round, inputs.hotkey)
    key = inputs.witness_storage_prefix + CallRecords.key(scope, request.id)
    raw = await optional_object(r2, r2.bucket, key)
    if raw is None:
        raise ValidationFailed("Model request does not match a recorded call")
    record = CallRecord.model_validate_json(raw)
    call = record.call
    if record.scope_key != scope.key or call.request != request:
        raise RuntimeError("Recorded call identity differs")
    if call.phase not in ("issued", "failed") or call.manifest_sha256 is None:
        raise RuntimeError("Recorded call has no complete capture")
    prefix = f"{inputs.witness_storage_prefix}rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
    with TemporaryDirectory(prefix="capture-", dir=directory) as temporary:
        capture = Path(temporary)
        for name in (
            "manifest.json",
            "client.bin",
            "server.bin",
            "authorization-server.bin",
            "claim.json",
            "request.json",
            "response.sse",
            "session.json",
        ):
            content = await read_object(r2, settings.r2_replay_bucket, prefix + name)
            await asyncio.to_thread((capture / name).write_bytes, content)
        manifest_bytes = await asyncio.to_thread((capture / "manifest.json").read_bytes)
        manifest = object_dict(json.loads(manifest_bytes))
        proof_hash = await asyncio.to_thread(
            lambda: hashlib.sha256((PROOF_PARAMETERS / "verifying.bin").read_bytes()).hexdigest()
        )
        bindings = {
            "call_id": request.id,
            "request_key": request.model_dump(),
            "round": scope.round,
            "hotkey": scope.hotkey,
            "exchange_id": call.exchange_id,
            "proof_key_sha256": proof_hash,
            "pricing_sha256": hashlib.sha256(pricing).hexdigest(),
        }
        if hashlib.sha256(manifest_bytes).hexdigest() != call.manifest_sha256 or any(
            manifest.get(key) != value for key, value in bindings.items()
        ):
            raise RuntimeError("Capture binding differs from the recorded call")
        verifier = await asyncio.create_subprocess_exec(
            "node",
            str(VERIFY_SCRIPT),
            "replay",
            str(capture),
            str(PROOF_PARAMETERS),
            request.provider,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/local/bin:" + os.defpath, "NODE_OPTIONS": "--max-old-space-size=384"},
        )
        try:
            async with asyncio.timeout(180):
                verified_bytes, _ = await verifier.communicate()
        finally:
            if verifier.returncode is None:
                verifier.kill()
            await verifier.wait()
        if verifier.returncode:
            raise RuntimeError("Captured TLS evidence did not verify")
        verified = VerifiedCapture.model_validate_json(verified_bytes)
        disclosed = await asyncio.to_thread((capture / "request.json").read_bytes)
        response = await asyncio.to_thread((capture / "response.sse").read_bytes)
        if (
            request_digest(disclosed) != request.request_sha256
            or hashlib.sha256(disclosed).hexdigest() != verified.request_sha256
            or hashlib.sha256(response).hexdigest() != verified.response_sha256
            or verified.session_id != request.session_id
            or any(manifest.get(key) != value for key, value in verified.model_dump().items() if key != "session_id")
        ):
            raise RuntimeError("Verified response differs from the sealed capture")
        model = object_dict(json.loads(disclosed)).get("model")
        if not isinstance(model, str):
            raise RuntimeError("Verified request has no model")
        if call.phase == "issued":
            if verified.usage is None:
                raise RuntimeError("Verified capture has no usable token usage")
            try:
                budget.consume(
                    request.id, model, verified.usage, not_dispatched=call.not_dispatched and not call.authorized
                )
            except BudgetRejected as error:
                raise RuntimeError("Verified usage and frozen pricing are inconsistent") from error
        return call.exchange_id, verified.provider_status, response


async def render_response(query: RenderCall, inputs: ValidationInputs, r2: R2Client, settings: Settings) -> bytes | str:
    request = httpx.Request("POST", "http://replay/v1/render/grid", params=query.query, json={"source": query.source})
    path, digest = request.url.raw_path.decode("ascii"), hashlib.sha256(request.content).hexdigest()
    root = f"{inputs.witness_storage_prefix}rounds/{inputs.submission.round}/{inputs.hotkey}/renders/"
    for version, identity in (("v2", (path, digest)), ("v1", (settings.renderer_id, path, digest))):
        key = root + version + "/" + hashlib.sha256(json_bytes(identity)).hexdigest()
        png = await optional_object(r2, settings.r2_replay_bucket, key + ".png")
        if png is not None:
            if not png.startswith(PNG_SIGNATURE):
                raise RuntimeError("Cached preview is not a PNG")
            return png
        rejection = await optional_object(r2, settings.r2_replay_bucket, key + ".rejection.json")
        if rejection is not None:
            return RenderRejected(rejection.decode("utf-8")).detail_json
    raise RuntimeError("No saved preview outcome for this render request")


async def validate_prompt(
    inputs: ValidationInputs,
    prompt: PromptImage,
    sandbox_root: Path,
    directory: Path,
    r2: R2Client,
    http: httpx.AsyncClient,
    settings: Settings,
) -> ValidationResult:
    image = await read_object(r2, r2.bucket, prompt.image_key)
    if hashlib.sha256(image).hexdigest() != prompt.sha256:
        raise RuntimeError("Frozen prompt bytes differ")
    image_path = directory / "prompt.png"
    await asyncio.to_thread(image_path.write_bytes, image)
    canonical = inputs.generations[prompt.id]
    expected = None
    if canonical.js is not None:
        response = await http.get(canonical.js, follow_redirects=True)
        response.raise_for_status()
        expected = response.content
    pricing = await read_object(r2, r2.bucket, f"pod-pricing/v1/{settings.witness_deployment}.json")
    budget = PromptBudget(pricing)
    try:
        async with (
            asyncio.timeout(settings.sandbox_timeout_seconds),
            Sandbox(sandbox_root, image_path, inputs.seed, settings) as sandbox,
        ):
            while True:
                message = await sandbox.receive()
                if isinstance(message, MinerResult):
                    if message.error:
                        raise ValidationFailed(f"Miner execution failed: {message.error}")
                    output = message.output.encode("utf-8") if message.output is not None else None
                    if output != expected:
                        raise ValidationFailed("Replayed JavaScript differs from the canonical output")
                    budget.accept()
                    return ValidationResult(verdict=ValidationVerdict.PASSED)
                if isinstance(message, ModelCall):
                    exchange, status, body = await model_response(
                        message, inputs, r2, settings, directory, pricing, budget
                    )
                    await sandbox.reply(exchange_id=exchange, status=status, body=encoded(body))
                else:
                    rendered = await render_response(message, inputs, r2, settings)
                    if isinstance(rendered, bytes):
                        await sandbox.reply(png=encoded(rendered))
                    else:
                        await sandbox.reply(rejection=rendered)
    except (ValidationFailed, BudgetRejected) as error:
        return ValidationResult(verdict=ValidationVerdict.FAILED, reason=str(error))
