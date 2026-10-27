"""Offline Responses replay using an operator-verified execution bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypeVar

from witness_client.calls import RequestKey
from witness_client.client import CapturedResponse
from witness_client.pods import CallView

from .accounting import PromptBudget
from .observations import (
    Execution,
    Generate,
    Inputs,
    Observation,
    ObservedWitness,
    ReplayMismatch,
    Schedule,
    invoke_generate,
)

R = TypeVar("R")


class ReplayWitness(ObservedWitness):
    def __init__(self, directory: str | Path, *, timeout: float = 30) -> None:
        self.directory = Path(directory)
        raw = (self.directory / "execution.json").read_bytes()
        self.bundle = json.loads((self.directory / "verified.json").read_bytes())
        execution = Execution.model_validate_json(raw)
        pricing = (self.directory / "pricing.json").read_bytes()
        if (
            self.bundle.get("version") != 2
            or not execution.finished
            or self.bundle.get("execution_sha256") != hashlib.sha256(raw).hexdigest()
            or self.bundle.get("pricing_sha256") != hashlib.sha256(pricing).hexdigest()
        ):
            raise ReplayMismatch(
                "Execution or frozen pricing changed after verification"
            )
        super().__init__(Schedule(execution, timeout=timeout))
        self.budget = PromptBudget(pricing)

    def read(
        self, entry: Observation, replay: bool, key: RequestKey, body: bytes
    ) -> CapturedResponse:
        if entry.exchange_id is None:
            raise ReplayMismatch("Observation has no captured response")
        item = self.bundle["captures"].get(entry.exchange_id)
        if not isinstance(item, dict):
            raise ReplayMismatch("Observation capture is missing")
        call = CallView.model_validate(item["call"])
        directory = self.directory / call.exchange_id
        manifest = (directory / "manifest.json").read_bytes()
        response = (directory / "response.sse").read_bytes()
        metadata = json.loads(manifest)
        if (
            call.request != key
            or call.phase != "issued"
            or hashlib.sha256(manifest).hexdigest() != call.manifest_sha256
            or metadata.get("response_sha256") != hashlib.sha256(response).hexdigest()
            or metadata.get("request_key") != key.model_dump()
            or metadata.get("call_id") != key.id
        ):
            raise ReplayMismatch("Replay evidence changed after verification")
        return CapturedResponse(call.exchange_id, metadata["provider_status"], response)

    def finish(self, output: str | None) -> None:
        self.schedule.finish(output)
        for entry in self.schedule.execution.observations:
            if entry.action != "create":
                continue
            item = self.bundle["captures"].get(entry.exchange_id)
            if not isinstance(item, dict):
                self.budget.consume(entry.request.id, "", None)
                continue
            call = CallView.model_validate(item["call"])
            if call.request != entry.request:
                raise ReplayMismatch("Accounting evidence belongs to a different call")
            self.budget.consume(
                entry.request.id,
                item["model"],
                item["usage"],
                not_dispatched=call.not_dispatched and not call.authorized,
            )
        self.budget.accept()


def run_replay(
    generate: Generate[R],
    image: bytes,
    seed: int,
    witness: ReplayWitness,
    renderer: R,
) -> str | None:
    if Inputs.supplied(image, seed) != witness.schedule.execution.inputs:
        raise ReplayMismatch("Replay image or generation seed differs")
    output = invoke_generate(generate, image, seed, witness, renderer)
    if output is not None and not isinstance(output, str):
        raise ValueError("generate() must return JavaScript or None")
    witness.finish(output)
    return output
