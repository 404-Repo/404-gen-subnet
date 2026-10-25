"""Opt-in checks of the packaged production Pod renderer and its lifecycle."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("WITNESS_TEST_POD_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Set WITNESS_TEST_POD_IMAGE to a locally built production Pod image"
)


def run(script: str) -> None:
    result = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
         "--cpus", "4", "--shm-size", "512m", "--entrypoint", "python", IMAGE, "-c", script],
        capture_output=True, timeout=150,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


def test_pod_contains_current_client_gateway_worker_and_renderer() -> None:
    expected = {}
    for source, target, pattern in (
        ("witness-client/src/witness_client", "/app/witness-client/src/witness_client", "*.py"),
        ("renderer-client/src/renderer_client", "/app/renderer-client/src/renderer_client", "*.py"),
        ("witness-gateway/pod_gateway", "/app/witness-gateway/pod_gateway", "*.py"),
        ("witness-server/worker", "/app/witness-server/worker", "*.py"),
        ("witness-server/verifier", "/app/witness-server/verifier", "*.mjs"),
        ("render-service-js/src", "/opt/renderer/src", "*.js"),
    ):
        for path in (ROOT / source).rglob(pattern):
            if "tests" in path.relative_to(ROOT / source).parts or path.name.endswith(".test.mjs"):
                continue
            expected[str(Path(target) / path.relative_to(ROOT / source))] = hashlib.sha256(path.read_bytes()).hexdigest()
    expected["/opt/renderer/package.json"] = hashlib.sha256((ROOT / "render-service-js/package.json").read_bytes()).hexdigest()
    run(f"""
import hashlib
from pathlib import Path
for name, digest in {expected!r}.items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest, name
assert Path('/opt/witness-affinity/libwitness-affinity.so').is_file()
assert not Path('/opt/benchmark').exists()
assert not Path('/opt/fixtures').exists()
""")


def test_renderer_readiness_determinism_and_worker_drain() -> None:
    run("""
import os, signal, threading
import httpx
from pod_gateway.renderer import render_service

drained = threading.Event()
source = 'export default function(THREE) { return new THREE.Mesh(new THREE.BoxGeometry(0.4,0.4,0.4),new THREE.MeshStandardMaterial({color:0x6688aa})); }'
with render_service(drained.set) as process:
    assert process is not None
    status = open(f'/proc/{process.pid}/status').read()
    assert 'Uid:\t1001\t1001\t1001\t1001' in status
    with httpx.Client(timeout=40, trust_env=False) as client:
        url = 'http://127.0.0.1:8002/render/grid?thetas=30&phis=0&img_size=64&lighting=neutral'
        first = client.post(url, json={'source': source})
        second = client.post(url, json={'source': source})
        assert first.status_code == second.status_code == 200, (first.text[:200],second.text[:200])
        assert first.content.startswith(bytes.fromhex('89504e470d0a1a0a'))
        assert first.content == second.content
    os.kill(process.pid, signal.SIGKILL)
    assert drained.wait(5), 'Renderer death must drain the worker'
assert process.poll() is not None
""")


def test_unready_renderer_startup_is_bounded() -> None:
    run("""
import time
from pod_gateway import renderer
renderer.READY_SECONDS = 0
started = time.monotonic()
try:
    with renderer.render_service(lambda: None):
        raise AssertionError('Unready renderer was accepted')
except TimeoutError:
    pass
assert time.monotonic() - started < 15
""")
