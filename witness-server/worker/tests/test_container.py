"""Opt-in checks against Docker's real context filtering and a built worker."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("WITNESS_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Set WITNESS_TEST_IMAGE to a locally built worker image"
)


def test_build_context_excludes_credentials_and_local_state(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
    (context / "Dockerfile.dockerignore").write_bytes(
        (WORKER / "Dockerfile.pods.dockerignore").read_bytes()
    )
    allowed = {
        "witness-client/LICENSE",
        "witness-client/THIRD_PARTY_NOTICES.md",
        "witness-client/native/main.go",
        "witness-client/native/go.mod",
        "witness-client/native/go.sum",
        "miner-runtime/src/miner_runtime/runner.py",
        "witness-server/worker/witness_pod/verifier/THIRD_PARTY_NOTICES.md",
        "witness-server/worker/witness_pod/verifier/verify.mjs",
        "witness-server/worker/witness_pod/verifier/package.json",
        "witness-server/worker/witness_pod/verifier/package-lock.json",
        "shared/subnet_common/witness/params/verifying.bin",
        "witness-server/worker/pyproject.toml",
        "witness-server/worker/witness_pod/worker.py",
        "witness-server/worker/witness_pod/channel.py",
        "witness-server/worker/witness_pod/timing.py",
    }
    excluded = {
        ".env",
        "generation-orchestrator/.env",
        "witness-gateway/.dev.vars",
        "witness-gateway/.wrangler/state/marker",
        "witness-server/worker/.env",
        "witness-server/worker/.venv/marker",
        "witness-server/worker/witness_pod/verifier/node_modules/marker",
        "witness-server/worker/witness_pod/verifier/protocol.test.mjs",
        "witness-server/worker/tests/test_tls.py",
        "witness-server/worker/witness_pod/verifier/params/proving.bin",
        "witness-server/worker/witness_pod/verifier/params/circuit.bin",
        "witness-client/native/privateauth",
        "witness-client/.env",
    }
    for relative in allowed | excluded:
        path = context / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic marker, no credential\n")
    output = tmp_path / "output"
    subprocess.run(
        [
            "docker",
            "build",
            "--network",
            "none",
            "--output",
            f"type=local,dest={output}",
            str(context),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert {
        str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()
    } == allowed


def test_worker_image_loads_runtime_and_matching_verification_key() -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--cpus",
            "2",
            "--mount",
            f"type=bind,source={WORKER.parents[1]},target=/source,readonly",
            "--workdir",
            "/tmp",
            "--entrypoint",
            "python",
            IMAGE,
            "/source/witness-server/worker/tests/container_smoke.py",
            "/source",
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
