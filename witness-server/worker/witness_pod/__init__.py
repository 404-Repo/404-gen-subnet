"""Witness Pod capture, rendering and replay service."""

from pathlib import Path

VERIFY_SCRIPT = Path(__file__).with_name("verify.mjs")
PROOF_PARAMETERS = Path(__file__).with_name("verifier") / "params"
