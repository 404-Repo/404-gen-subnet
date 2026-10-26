"""Opt-in checks against Docker's real context filtering and a built worker."""

from __future__ import annotations

import hashlib
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
        "witness-server/verifier/verify.mjs",
        "witness-server/verifier/package.json",
        "witness-server/verifier/package-lock.json",
        "witness-server/verifier/params/verifying.bin",
        "witness-server/worker/worker.py",
        "witness-server/worker/channel.py",
        "witness-server/worker/timing.py",
    }
    excluded = {
        ".env",
        "generation-orchestrator/.env",
        "witness-gateway/.dev.vars",
        "witness-gateway/.wrangler/state/marker",
        "witness-server/worker/.env",
        "witness-server/worker/.venv/marker",
        "witness-server/verifier/node_modules/marker",
        "witness-server/verifier/protocol.test.mjs",
        "witness-server/worker/tests/test_tls.py",
        "witness-server/verifier/params/proving.bin",
        "witness-server/verifier/params/circuit.bin",
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
    expected = hashlib.sha256(
        (WORKER.parent / "verifier/params/verifying.bin").read_bytes()
    ).hexdigest()
    licences = {
        name: hashlib.sha256(
            (WORKER.parents[1] / "witness-client" / name).read_bytes()
        ).hexdigest()
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")
    }
    script = f"""
import hashlib
from pathlib import Path
import subprocess
import worker

assert worker.HERE == Path('/app/witness-server/worker')
assert worker.WITNESS == Path('/app/witness-server/verifier')
assert hashlib.sha256((worker.PARAMETERS / 'verifying.bin').read_bytes()).hexdigest() == {expected!r}
assert not (worker.PARAMETERS / 'proving.bin').exists()
assert not (worker.PARAMETERS / 'circuit.bin').exists()
assert not list(worker.WITNESS.glob('*.test.mjs'))
assert not (worker.HERE / 'tests').exists()
assert Path('/app/witness-client/native/privateauth').is_file()
for name, digest in {licences!r}.items():
    assert hashlib.sha256((Path('/app/licenses') / name).read_bytes()).hexdigest() == digest
subprocess.run(['node', '--input-type=module', '-e',
                "await import('../verifier/verify.mjs'); await import('../verifier/gate.mjs')"], check=True)
"""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--workdir",
            "/app/witness-server/worker",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
