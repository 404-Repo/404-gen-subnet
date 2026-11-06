"""The packaged renderer runs alongside the worker with its own credentials and limits."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx
from renderer_client import RenderOptions, RenderRecord, RenderRejected, RenderRequest, RenderUnavailable
from renderer_client.client import MAX_ERROR_BYTES, MAX_RESPONSE_BYTES

from .artifacts import Objects
from .models import Scope, json_bytes

if TYPE_CHECKING:
    from .artifacts import ArtifactProfile

READY_SECONDS = 60
MAX_SUCCESSFUL_RENDERS = 10_000


def render_options(query: Mapping[str, str]) -> RenderOptions:
    """Expand the grid endpoint's defaults and scalar phi into effective options."""
    defaults = RenderOptions()
    thetas = tuple(float(value) for value in query["thetas"].split(",")) if query.get("thetas") else defaults.thetas
    phis = tuple(float(value) for value in query.get("phis", "").split(",")) if query.get("phis") else (-15.0,)
    if len(phis) == 1:
        phis *= len(thetas)
    lighting = query.get("lighting", defaults.lighting)
    if lighting not in ("follow", "studio", "neutral"):
        raise RenderRejected(json_bytes({
            "error": f"unknown lighting '{lighting}'; expected one of: follow, studio, neutral",
        }).decode())
    return RenderOptions(
        thetas=tuple(value or 0.0 for value in thetas),
        phis=tuple(value or 0.0 for value in phis),
        img_size=int(query.get("img_size") or defaults.img_size) or defaults.img_size,
        cam_radius=float(query.get("cam_radius") or defaults.cam_radius) or defaults.cam_radius,
        cam_fov_deg=float(query.get("cam_fov_deg") or defaults.cam_fov_deg) or defaults.cam_fov_deg,
        gap=int(query.get("gap") or defaults.gap),
        bg_color=query.get("bg_color") or defaults.bg_color,
        lighting=lighting,
    )


class RenderObjects(Objects, Protocol):
    def keys(self, prefix: str) -> Iterator[str]: ...


class RenderCache:
    """Original render outcomes and a startup-restored, in-memory success count."""

    def __init__(self, objects: RenderObjects, scope: Scope, renderer_id: str, *, storage_prefix: str = "") -> None:
        self.objects, self.scope, self.renderer_id = objects, scope, renderer_id
        self.prefix = f"{storage_prefix}rounds/{scope.round}/{scope.hotkey}/renders/v1/"
        saved = set(objects.keys(self.prefix))
        self._pngs = {key for key in saved if key.endswith(".png")}
        self._rejections = {key for key in saved if key.endswith(".rejection.json")}
        self._save_lock = threading.Lock()

    def request(self, source: str, options: RenderOptions) -> RenderRequest:
        return RenderRequest(self.renderer_id, hashlib.sha256(source.encode()).hexdigest(), options)

    def key(self, request: RenderRequest) -> str:
        return self.prefix + hashlib.sha256(json_bytes(asdict(request))).hexdigest()

    @property
    def full(self) -> bool:
        return len(self._pngs) >= MAX_SUCCESSFUL_RENDERS

    def get(self, request: RenderRequest) -> RenderRecord | None:
        key = self.key(request)
        try:
            if key + ".png" in self._pngs:
                png = self.objects.get(key + ".png", MAX_RESPONSE_BYTES)
                if png is None:
                    raise RenderUnavailable("Saved PNG is missing")
                return RenderRecord(request, png=png)
            if key + ".rejection.json" in self._rejections:
                rejection = self.objects.get(key + ".rejection.json", MAX_ERROR_BYTES)
                if rejection is None:
                    raise RenderUnavailable("Saved render rejection is missing")
                return RenderRecord(request, rejection_json=rejection.decode())
        except ValueError as error:
            raise RenderUnavailable("Invalid saved render outcome") from error
        return None

    def save(self, record: RenderRecord) -> bool:
        """Return false when a new PNG would exceed the successful-render allowance."""
        key = self.key(record.request)
        with self._save_lock:
            if record.png is not None:
                if key + ".png" in self._pngs:
                    return True
                if self.full:
                    return False
                self.objects.put(key + ".png", record.png, "image/png")
                self._pngs.add(key + ".png")
            else:
                assert record.rejection_json is not None
                self.objects.put(key + ".rejection.json", record.rejection_json.encode(), "application/json")
                self._rejections.add(key + ".rejection.json")
        return True


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
