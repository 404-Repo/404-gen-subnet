import asyncio
import base64
import math
import os
import shutil
import signal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from subnet_common.witness.models import json_bytes

from validation_worker.settings import Settings


class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["model"]
    body: str
    request_id: int = 0
    session_id: str | None = None


class RenderCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["render"]
    source: str = Field(max_length=1024 * 1024)
    query: dict[str, str]


class MinerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["result"]
    output: str | None = Field(default=None, max_length=1024 * 1024)
    error: str = Field(default="", max_length=2048)


Requests: TypeAdapter[ModelCall | RenderCall | MinerResult] = TypeAdapter(
    Annotated[ModelCall | RenderCall | MinerResult, Field(discriminator="operation")]
)


def prepare_sandbox(source: Path, directory: Path, settings: Settings) -> Path:
    """Copy the credential-free runtime and submitted source for one reusable slot."""
    template = settings.sandbox_root.resolve()
    if (
        shutil.which("nsjail") is None
        or os.geteuid() != 0
        or template == Path("/")
        or not (template / "usr/local/bin/python").is_file()
    ):
        raise RuntimeError("NsJail, a root worker and a pristine SANDBOX_ROOT are required")
    root = directory / "jail"
    shutil.copytree(template, root, symlinks=True)
    shutil.copytree(source, root / "source", dirs_exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("miner.py"), root / "runner.py")
    replay = root / "replay" / "validation_worker"
    replay.mkdir(parents=True)
    for name in ("__init__.py", "render_recovery.py"):
        shutil.copyfile(Path(__file__).with_name(name), replay / name)
    (root / "scratch").mkdir(exist_ok=True)
    root.chmod(0o755)
    for entry in root.rglob("*"):
        if not entry.is_symlink():
            entry.chmod(0o755 if entry.is_dir() or entry.stat().st_mode & 0o111 else 0o644)
    return root


class Sandbox:
    """One credential-free miner process inside a prepared jail."""

    def __init__(self, root: Path, image: Path, seed: int, settings: Settings, slot: int = 0) -> None:
        self.root, self.image, self.seed, self.settings = root, image, seed, settings
        self.uid = 40000 + slot
        self.process: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> "Sandbox":
        binary = shutil.which("nsjail")
        if binary is None:
            raise RuntimeError("NsJail is required")
        scratch = self.root / "scratch"
        await asyncio.to_thread(shutil.rmtree, scratch)
        await asyncio.to_thread(scratch.mkdir, mode=0o700)
        await asyncio.to_thread(os.chown, scratch, self.uid, self.uid)
        await asyncio.to_thread(shutil.copyfile, self.image, self.root / "prompt.png")
        self.process = await asyncio.create_subprocess_exec(
            binary,
            "--mode",
            "o",
            "--quiet",
            "--chroot",
            str(self.root),
            "--cwd",
            "/source",
            "--disable_proc",
            "--disable_clone_newnet",
            "--disable_clone_newuser",
            "--disable_clone_newns",
            "--disable_clone_newpid",
            "--disable_clone_newipc",
            "--disable_clone_newuts",
            "--disable_clone_newcgroup",
            "--user",
            str(self.uid),
            "--group",
            str(self.uid),
            "--skip_setsid",
            "--nice_level",
            "0",
            "--seccomp_policy",
            str(Path(__file__).with_name("sandbox.kafel")),
            "--rlimit_as",
            "4096",
            "--rlimit_cpu",
            str(math.ceil(self.settings.sandbox_timeout_seconds)),
            "--rlimit_fsize",
            "16",
            "--rlimit_nofile",
            "128",
            "--rlimit_nproc",
            "128",
            "--rlimit_core",
            "0",
            "--time_limit",
            str(math.ceil(self.settings.sandbox_timeout_seconds)),
            "--env",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "--env",
            "HOME=/scratch",
            "--env",
            "TMPDIR=/scratch",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONPATH=/replay",
            "--env",
            "PYTHONHASHSEED=0",
            "--env",
            "OPENBLAS_NUM_THREADS=1",
            "--env",
            "OMP_NUM_THREADS=1",
            "--env",
            "MKL_NUM_THREADS=1",
            "--",
            "/usr/local/bin/python",
            "-P",
            "-s",
            "-u",
            "/runner.py",
            str(self.seed),
            self.settings.witness_provider,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
            env={"PATH": os.defpath},
            start_new_session=True,
        )
        return self

    async def receive(self) -> ModelCall | RenderCall | MinerResult:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Sandbox is not started")
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"Sandbox exited without a result: {await self.process.wait()}")
        return Requests.validate_json(line)

    async def reply(self, **fields: object) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Sandbox is not started")
        self.process.stdin.write(json_bytes(fields) + b"\n")
        await self.process.stdin.drain()

    async def __aexit__(self, *_: object) -> None:
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.communicate()


def encoded(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")
