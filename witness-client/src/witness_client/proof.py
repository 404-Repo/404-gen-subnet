"""Direct binding to the credential prover. The TLS payload never enters it."""

import ctypes
import json
import sys
from contextlib import suppress
from os import PathLike
from pathlib import Path
from typing import NotRequired, TypedDict

from .timing import emit, measure
from .types import CredentialProof, PrivateRecord, ProofBundle


class NativeTimings(TypedDict):
    duration_ms: float
    steps_ms: dict[str, float]
    parameters_cached: bool


class ProofResult(TypedDict):
    bundle: NotRequired[ProofBundle]
    error: NotRequired[str]
    proof_key_sha256: NotRequired[str]
    timings: NotRequired[NativeTimings]


class Prover:
    def __init__(self, parameters: str | PathLike[str]) -> None:
        self.parameters = str(Path(parameters).resolve()).encode()
        suffix = "dylib" if sys.platform == "darwin" else "so"
        path = Path(__file__).with_name(f"libwitnessproof.{suffix}")
        if not path.is_file():
            raise RuntimeError(
                "Install a built 404-witness-client wheel (native proof library missing)"
            )
        with measure("native_library_load"):
            self.library = ctypes.CDLL(str(path))
            self.library.WitnessProofABI.restype = ctypes.c_int
            if self.library.WitnessProofABI() != 2:
                raise RuntimeError("Unsupported witness proof library ABI")
            self.library.WitnessProof.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            self.library.WitnessProof.restype = ctypes.c_void_p
            self.library.WitnessProofFree.argtypes = [ctypes.c_void_p]
            self.library.WitnessProofFree.restype = None
            self.library.WitnessProofInit.argtypes = [ctypes.c_char_p]
            self.library.WitnessProofInit.restype = ctypes.c_void_p
        with measure("native_initialize_call"):
            pointer: int | None = self.library.WitnessProofInit(self.parameters)
        if not pointer:
            raise RuntimeError("Proof library returned no initialization result")
        try:
            result: ProofResult = json.loads(ctypes.string_at(pointer))
        finally:
            self.library.WitnessProofFree(pointer)
        if "error" in result:
            raise RuntimeError(result["error"])
        self.verifying_key_sha256 = result["proof_key_sha256"]
        _native_timings("parameter_initialize", result)

    def prove(self, private_records: list[PrivateRecord]) -> CredentialProof:
        if len(private_records) != 2:
            raise ValueError("Expected exactly the credential and its key update")
        token, update = private_records
        if (token["number"], update["number"]) != (0, 1) or any(
            token[k] != update[k] for k in ("key", "nonce")
        ):
            raise ValueError("Private credential epoch mismatch")
        raw = json.dumps(
            {
                "key": token["key"],
                "nonce": token["nonce"],
                "token_record": token["record"],
                "update_record": update["record"],
            }
        ).encode()
        with measure("native_prove_call"):
            pointer: int | None = self.library.WitnessProof(
                self.parameters, raw, len(raw)
            )
        if not pointer:
            raise RuntimeError("Proof library returned no result")
        try:
            result: ProofResult = json.loads(ctypes.string_at(pointer))
        finally:
            self.library.WitnessProofFree(pointer)
        if "error" in result:
            raise RuntimeError(f"Credential proof failed: {result['error']}")
        _native_timings("proof_generate", result)
        return {"nonce": token["nonce"], "bundle": result["bundle"]}


def _native_timings(phase: str, result: ProofResult) -> None:
    # Diagnostics are optional ABI fields, never part of the credential claim.
    with suppress(Exception):
        if timings := result.get("timings"):
            emit(
                phase,
                0,
                duration_ms=timings["duration_ms"],
                steps_ms=timings["steps_ms"],
                parameters_cached=timings["parameters_cached"],
            )
