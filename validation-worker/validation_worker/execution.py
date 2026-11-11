import asyncio
import base64
import hashlib
import json
import os
from copy import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pydantic import BaseModel
from renderer_client import RenderRejected, RenderUnavailable
from renderer_client.client import PNG_SIGNATURE
from subnet_common.competition.validation import ValidationResult, ValidationVerdict
from subnet_common.r2_client import R2Client
from subnet_common.witness.models import Scope, json_bytes, object_dict
from subnet_common.witness.pricing import BudgetRejected, PromptBudget
from subnet_common.witness.rounds import PromptImage
from witness_client import WitnessError
from witness_client.calls import RequestKey, request_digest
from witness_pod import PROOF_PARAMETERS, VERIFY_SCRIPT
from witness_pod.records import CallRecord, CallRecords

from validation_worker.inputs import ValidationInputs
from validation_worker.preparation import optional_object, read_object, verify_recorded_failure
from validation_worker.render_recovery import (
    MAX_MISSING_RENDER_CALLS,
    MAX_MISSING_RENDER_RETRIES,
    MissingRenderAttempt,
    MissingRenderOutcome,
    RenderFailure,
    RenderRecoverySearch,
)
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
    budget: PromptBudget,
) -> tuple[str, int, bytes] | WitnessError | None:
    body = base64.b64decode(query.body, validate=True)
    request = RequestKey.from_body(settings.witness_provider, body, query.request_id, session_id=query.session_id)
    scope = Scope(settings.witness_deployment, inputs.submission.round, inputs.hotkey)
    key = inputs.witness_storage_prefix + CallRecords.key(scope, request.id)
    raw = await optional_object(r2, r2.bucket, key)
    if raw is None:
        return None
    record = CallRecord.model_validate_json(raw)
    call = record.call
    if record.scope_key != scope.key or call.request != request:
        raise ValueError("Recorded call identity differs")
    prefix = f"{inputs.witness_storage_prefix}rounds/{scope.round}/{scope.hotkey}/{call.exchange_id}/"
    if (
        call.phase == "failed"
        and call.manifest_sha256 is None
        and (call.authorized or call.not_dispatched)
        and call.failure is not None
    ):
        failure = await optional_object(r2, settings.r2_replay_bucket, prefix + "failure.json")
        if failure is None:
            failure = await read_object(r2, settings.r2_replay_bucket, prefix + "status.json")
        # The matched journal binds the request for a failure before dispatch.
        request_bytes = body
        if call.authorized:
            request_bytes = await read_object(r2, settings.r2_replay_bucket, prefix + "request.json")
        verify_recorded_failure(call, request_bytes, failure)
        return WitnessError(
            "Recorded witness capture failed", call.exchange_id, code="capture_failed", source="gateway"
        )
    if call.phase not in ("issued", "failed") or call.manifest_sha256 is None:
        raise RuntimeError("Recorded call has no complete capture")
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
        }
        if hashlib.sha256(manifest_bytes).hexdigest() != call.manifest_sha256 or any(
            manifest.get(key) != value for key, value in bindings.items()
        ):
            raise ValueError("Capture binding differs from the recorded call")
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
        if verifier.returncode and (verifier.returncode < 0 or verifier.returncode >= 128):
            raise RuntimeError("Capture verifier process was interrupted")
        if verifier.returncode:
            raise ValueError("Captured TLS evidence did not verify")
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
            raise ValueError("Verified response differs from the sealed capture")
        model = object_dict(json.loads(disclosed)).get("model")
        if not isinstance(model, str):
            raise ValueError("Verified request has no model")
        if call.phase == "issued":
            if verified.usage is None:
                raise ValueError("Verified capture has no usable token usage")
            try:
                budget.consume(
                    request.id, model, verified.usage, not_dispatched=call.not_dispatched and not call.authorized
                )
            except BudgetRejected as error:
                raise ValueError("Verified usage and frozen pricing are inconsistent") from error
        if call.phase == "failed" and verified.provider_status == 200:
            return WitnessError("Unusable provider response", call.exchange_id, code="capture_failed", source="gateway")
        return call.exchange_id, verified.provider_status, response


async def render_response(query: RenderCall, inputs: ValidationInputs, r2: R2Client, settings: Settings) -> bytes | str:
    request = httpx.Request("POST", "http://replay/v1/render/grid", params=query.query, json={"source": query.source})
    path, digest = request.url.raw_path.decode("ascii"), hashlib.sha256(request.content).hexdigest()
    root = f"{inputs.witness_storage_prefix}rounds/{inputs.submission.round}/{inputs.hotkey}/renders/"
    v2 = root + "v2/" + hashlib.sha256(json_bytes((path, digest))).hexdigest()
    v1 = root + "v1/" + hashlib.sha256(json_bytes((settings.renderer_id, path, digest))).hexdigest()
    for key in (v2, v1):
        png = await optional_object(r2, settings.r2_replay_bucket, key + ".png")
        if png is not None:
            if not png.startswith(PNG_SIGNATURE):
                raise ValueError("Cached preview is not a PNG")
            return png
        rejection = await optional_object(r2, settings.r2_replay_bucket, key + ".rejection.json")
        if rejection is not None:
            return RenderRejected(rejection.decode("utf-8")).detail_json
    raise MissingRenderOutcome(v2)


async def validate_prompt(
    inputs: ValidationInputs,
    prompt: PromptImage,
    sandbox_root: Path,
    directory: Path,
    r2: R2Client,
    http: httpx.AsyncClient,
    settings: Settings,
    budget: PromptBudget,
) -> ValidationResult:
    canonical = inputs.generations[prompt.id]
    if canonical.js is None and canonical.failure_reason != "generation_failed":
        raise ValueError(f"No completed independent submission: {canonical.failure_reason or 'missing_saved_output'}")

    image = await read_object(r2, r2.bucket, prompt.image_key)
    if hashlib.sha256(image).hexdigest() != prompt.sha256:
        raise ValueError("Frozen prompt bytes differ")
    image_path = directory / "prompt.png"
    await asyncio.to_thread(image_path.write_bytes, image)
    expected = None
    if canonical.js is not None:
        response = await http.get(canonical.js, follow_redirects=True)
        response.raise_for_status()
        expected = response.content

    search = RenderRecoverySearch()
    while (plan := search.next_plan()) is not None:
        missing: list[MissingRenderAttempt] = []
        unrecorded_model_call = False
        trial_budget = copy(budget)
        trial_budget.charges = {}
        try:
            async with Sandbox(sandbox_root, image_path, inputs.seed, settings) as sandbox:
                while True:
                    message = await sandbox.receive()
                    if isinstance(message, MinerResult):
                        if message.error:
                            raise ValidationFailed(f"Miner execution failed: {message.error}")
                        output = message.output.encode("utf-8") if message.output is not None else None
                        if output != expected:
                            raise ValidationFailed("Replayed JavaScript differs from the canonical output")
                        trial_budget.accept()
                        break
                    if isinstance(message, ModelCall):
                        captured = await model_response(message, inputs, r2, settings, directory, trial_budget)
                        if captured is None:
                            unrecorded_model_call = True
                            await sandbox.reply(
                                error="capture_unrecorded", message="No recorded capture for this request"
                            )
                            continue
                        if isinstance(captured, WitnessError):
                            await sandbox.reply(
                                error=captured.code, exchange_id=captured.exchange_id, message=str(captured)
                            )
                            continue
                        exchange, status, body = captured
                        if not (missing or unrecorded_model_call):
                            budget.charges.update(trial_budget.charges)
                        await sandbox.reply(exchange_id=exchange, status=status, body=encoded(body))
                        continue
                    try:
                        rendered = await render_response(message, inputs, r2, settings)
                    except MissingRenderOutcome as error:
                        occurrence = sum(item.request_key == error.request_key for item in missing)
                        if occurrence >= MAX_MISSING_RENDER_RETRIES or len(missing) >= MAX_MISSING_RENDER_CALLS:
                            raise ValidationFailed("Missing render trial limit reached") from error
                        sequence = plan.get(error.request_key, (RenderFailure.CONNECTION,))
                        failure = sequence[min(occurrence, len(sequence) - 1)]
                        missing.append(MissingRenderAttempt(error.request_key, occurrence, failure))
                        await sandbox.reply(unavailable=str(error), failure_kind=failure.value)
                    else:
                        if isinstance(rendered, bytes):
                            await sandbox.reply(png=encoded(rendered))
                        else:
                            await sandbox.reply(rejection=rendered)
        except (ValidationFailed, BudgetRejected) as error:
            if not missing:
                if unrecorded_model_call:
                    raise ValueError(f"Replay with an unrecorded Witness call did not pass ({error})") from error
                return ValidationResult(verdict=ValidationVerdict.FAILED, reason=str(error))
            search.observe(plan, missing)
            continue

        assumptions = []
        if missing:
            failures = ", ".join(sorted({item.failure.value for item in missing}))
            assumptions.append(f"render errors {failures}")
        if unrecorded_model_call:
            assumptions.append("Witness connection failures")
        if assumptions and expected is not None:
            views = (
                f"{inputs.witness_storage_prefix}rounds/{inputs.submission.round}/"
                f"{inputs.hotkey}/submitted/{prompt.id}"
            )
            if urlsplit(canonical.views or "").path.strip("/") != views:
                raise ValueError("Canonical views do not match the saved submission")
            grid = await read_object(r2, r2.bucket, views + "/grid.png")
            if not grid.startswith(PNG_SIGNATURE):
                raise ValueError("Saved final grid is not a PNG")

        budget.charges.clear()
        budget.charges.update(trial_budget.charges)
        reason = ""
        if assumptions:
            reason = f"Recovered in trial {search.attempts} using assumed {' and '.join(assumptions)}"
            logger.info(f"{reason} for prompt {prompt.id}")
        return ValidationResult(verdict=ValidationVerdict.PASSED, reason=reason)

    raise RenderUnavailable(f"Missing preview recovery exhausted after {search.attempts} trials")
