"""Content-free failure facts and immutable Pod diagnostic records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class StackFrame(Frozen):
    file: str
    function: str
    line: int


class VerifierFrame(Frozen):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    file: Annotated[str, Field(pattern=r"^(worker|verifier)/[A-Za-z0-9_.-]+\.mjs$")]
    function: Annotated[str, Field(pattern=r"^[A-Za-z_$][A-Za-z0-9_.$]*$")] | None
    line: Annotated[int, Field(gt=0)]
    column: Annotated[int, Field(gt=0)]


class VerifierEvent(Frozen):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    event: Literal["witness_verifier_failure"]
    mode: str
    error_type: Annotated[str, Field(pattern=r"^[A-Za-z_$][A-Za-z0-9_$]*$")]
    stack: Annotated[tuple[VerifierFrame, ...], Field(max_length=32)]


class VerifierFailure(Frozen):
    mode: str
    returncode: int | None
    error_type: str | None = None
    stack: tuple[VerifierFrame, ...] = ()


def verifier_failure(stderr: bytes, mode: str, returncode: int | None) -> VerifierFailure:
    """Validate the verifier's safe event once; never retain other stderr."""
    for line in stderr.splitlines():
        try:
            event = VerifierEvent.model_validate_json(line)
        except ValidationError:
            continue
        if event.mode == mode:
            return VerifierFailure(mode=mode, returncode=returncode, error_type=event.error_type, stack=event.stack)
    return VerifierFailure(mode=mode, returncode=returncode)


class FailureFact(Frozen):
    stage: str
    reason: str
    error_type: str | None = None
    cause_type: str | None = None
    stack: tuple[StackFrame, ...] = ()
    verifier: VerifierFailure | None = None
    provider_status: int | None = None


def failure_fact(
    error: Exception, stage: str, *, reason: str | None = None, verifier: VerifierFailure | None = None,
) -> FailureFact:
    frames = []
    trace = error.__traceback__
    while trace is not None:
        frames.append(StackFrame(
            file="/".join(trace.tb_frame.f_code.co_filename.replace("\\", "/").split("/")[-2:]),
            function=trace.tb_frame.f_code.co_name,
            line=trace.tb_lineno,
        ))
        trace = trace.tb_next
    return FailureFact(
        stage=stage, reason=reason or stage + "_failed", error_type=type(error).__name__,
        cause_type=None if error.__cause__ is None else type(error.__cause__).__name__,
        stack=tuple(frames[-32:]), verifier=verifier,
    )


@dataclass
class FailureObservation:
    """Keep the originating fact within one capture or recovery attempt."""

    failure: FailureFact | None = None

    def observe(self, failure: FailureFact) -> None:
        if self.failure is None:
            self.failure = failure


class RuntimeIdentity(Frozen):
    python_version: str
    pod_spec_sha256: str | None


class ExchangeIdentity(Frozen):
    deployment: str
    round: str
    hotkey: str
    call_id: str
    exchange_id: str
    pod_id: str
    pod_start_id: str
    proof_key_sha256: str
    pricing_sha256: str
    runtime: RuntimeIdentity


Operation = Literal["capture", "recovery", "outcome"]


class DiagnosticRecord(Frozen):
    version: Literal[1] = 1
    observed_at_ns: int
    operation: Operation
    identity: ExchangeIdentity
    failure: FailureFact

    def record_id(self) -> str:
        """Repeated observations of the same failure reuse the first saved record."""
        payload = self.model_dump_json(exclude={"observed_at_ns"}).encode()
        return hashlib.sha256(payload).hexdigest()
