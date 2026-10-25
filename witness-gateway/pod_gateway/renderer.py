"""The packaged renderer runs alongside the worker with its own credentials and limits."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .models import json_bytes

if TYPE_CHECKING:
    from .artifacts import ArtifactProfile

READY_SECONDS = 60


def renderer_profile_sha256(root: Path) -> str:
    """Identify the packaged JavaScript renderer and its locked dependencies."""
    paths = sorted(path for path in (root / "src").rglob("*") if path.suffix in {".js", ".html"})
    if not paths:
        raise ValueError("Packaged renderer sources are missing")
    paths.append(root / "package-lock.json")
    manifest = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return hashlib.sha256(json_bytes(manifest)).hexdigest()


def require_artifact_profile(profile: ArtifactProfile, root: Path) -> None:
    if profile.bootstrap_sha256 is not None or profile.validation_sha256 != renderer_profile_sha256(root):
        raise ValueError("Artifact profile differs from the packaged renderer")


@contextmanager
def render_service(drain: Callable[[], None]) -> Iterator[subprocess.Popen[bytes] | None]:
    if os.environ.get("WITNESS_RENDERER_ENABLED") != "1":
        yield None
        return
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PUPPETEER_EXECUTABLE_PATH": "/usr/bin/chromium",
    }
    subprocess.run(
        ["node", "--import", "/opt/renderer/src/software-rendering.js", "-e", ""],
        env=environment,
        check=True,
    )
    Path("/usr/lib/chromium/SwiftShader.ini").chmod(0o644)
    with tempfile.TemporaryDirectory(prefix="witness-renderer-") as home:
        os.chown(home, 1001, 1001)
        environment.update(
            HOME=home,
            PORT="8002",
            STATIC_PORT="3000",
            RENDER_POOL_SIZE="1",
            VALIDATION_POOL_SIZE="1",
            RENDER_TIMEOUT_MS="30000",
            VALIDATION_TIMEOUT_MS="5000",
            RENDER_QUEUE_CAPACITY="8",
            RENDER_QUEUE_WAIT_MS="120000",
            LD_PRELOAD="/opt/witness-affinity/libwitness-affinity.so",
        )
        process = subprocess.Popen(
            ["node", "src/server.js"],
            cwd="/opt/renderer",
            env=environment,
            user=1001,
            group=1001,
            extra_groups=[],
            start_new_session=True,
        )
        stopping = threading.Event()

        def watch() -> None:
            process.wait()
            if not stopping.is_set():
                drain()

        watcher: threading.Thread | None = None
        try:
            deadline = time.monotonic() + READY_SECONDS
            with httpx.Client(timeout=1, trust_env=False) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Renderer exited during startup")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Renderer readiness deadline reached")
                    try:
                        if client.get("http://127.0.0.1:8002/ping").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.25)
            watcher = threading.Thread(target=watch, name="witness-renderer", daemon=True)
            watcher.start()
            yield process
        finally:
            stopping.set()
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if watcher is not None:
                watcher.join()
