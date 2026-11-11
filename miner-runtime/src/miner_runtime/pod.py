"""Self-starting generation Pods and their service-owned job contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from witness_client import AsyncWitnessClient

IMAGE_PATTERN = r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
IMAGE_ROOT = Path("/opt/leader-image")
IMAGE_PARAMETERS = IMAGE_ROOT / "params"
IMAGE_MANIFEST = IMAGE_ROOT / "provenance.json"
REMOTE = Path("/opt/leader-run")
PARAMS_PATH = "witness-server/worker/witness_pod/verifier/params"
PARAMETERS = {
    "circuit.bin": "451b8ebe434e1b9cee5982a46f4630d3d3647fe49c11f2ec6d88b8731617f9c7",
    "proving.bin": "31bd198d1325f526f5f1add2272bf73ffaff23c9e2d0ac7463c0f5e88c8701e7",
    "verifying.bin": "6478059e4ad0d39e8c157c76cea9c6dd777c2ab5143cf8694876094facf39b16",
}


def https(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Expected an HTTPS URL without credentials, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    generation_image: str
    source_repository: str
    source_commit: str
    prompts: dict[str, str]
    seed: int
    hotkey: str
    account_id: str
    witness_url: str = "https://witness.subnet404.xyz"
    cpu_id: str = "cpu3c"
    vcpus: int = 8
    disk_gb: int = 20
    data_center: str | None = None
    workers: int = 24
    parallel_calls: int = 32
    parallel_preparations: int = 4
    prover_contexts: int = 4
    go_cpus: int = 8
    go_memory_mib: int = 2048
    round_inputs_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise ValueError("Use a full 40-character source commit")
        if not re.fullmatch(IMAGE_PATTERN, self.generation_image):
            raise ValueError("Generation image must be pinned by SHA256 digest")
        for digest_value in (self.round_inputs_fingerprint,):
            if digest_value is not None and not re.fullmatch(r"[0-9a-f]{64}", digest_value):
                raise ValueError("Expected a SHA256 digest")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", self.source_repository):
            raise ValueError("Source must be an HTTPS GitHub repository URL")
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", self.hotkey):
            raise ValueError("Expected an operator hotkey address")
        if not re.fullmatch(r"[a-f0-9-]{1,80}", self.account_id):
            raise ValueError("Expected a subscription account identifier")
        https(self.witness_url)
        if urlsplit(self.witness_url).path not in ("", "/"):
            raise ValueError("Witness URL must be an HTTPS origin")
        if not 1 <= len(self.prompts) <= 4096 or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", key)
                or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in self.prompts.items()):
            raise ValueError("Expected official prompt IDs and image SHA256 values")
        if not 0 <= self.seed < 2**64:
            raise ValueError("Seed must be an unsigned 64-bit integer")
        if self.vcpus < 2 or self.vcpus > 32 or self.vcpus & (self.vcpus - 1):
            raise ValueError("vcpus must be a power of two from 2 through 32")
        if not 1 <= self.go_cpus <= self.vcpus or self.go_memory_mib < 768 or self.disk_gb < 10:
            raise ValueError("Invalid runner CPU, memory or disk limits")
        if not 1 <= self.prover_contexts <= min(8, self.parallel_preparations):
            raise ValueError("Prover contexts must fit preparation slots")
        if not 1 <= self.parallel_preparations <= self.parallel_calls <= 32:
            raise ValueError("Preparation slots must fit active calls (1-32)")
        if self.workers < 1:
            raise ValueError("Invalid workers")


def pod_body(config: Config, *, hotkey: str, access_token: str, name: str | None = None) -> dict[str, object]:
    if not hotkey or not access_token:
        raise ValueError("A job hotkey and access token are required")
    name = name or f"leader-runner-{uuid.uuid4()}"
    if not re.fullmatch(r"leader-runner-[A-Za-z0-9-]{1,64}", name):
        raise ValueError("Invalid generation Pod name")
    result: dict[str, object] = {
        "name": name, "image": config.generation_image, "cloud": "SECURE",
        "cpu": {"id": config.cpu_id, "vcpuCount": config.vcpus}, "disk": config.disk_gb,
        "startSsh": False,
        "env": {
            "LEADER_JOB_CONFIG": json.dumps(asdict(config)),
            "LEADER_JOB_HOTKEY": hotkey,
            "LEADER_JOB_ACCESS_TOKEN": access_token,
            "LOGURU_DIAGNOSE": "NO",
        },
    }
    if config.data_center:
        result["dataCenterIds"] = [config.data_center]
    return result



def execute(argv: list[str], *, cwd: Path | None = None, data: bytes | None = None,
            timeout: int = 60, capture: bool = True) -> bytes:
    result = subprocess.run(argv, cwd=cwd, input=data, check=False,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        # Subprocess failures can echo input or environment values.
        raise RuntimeError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout or b""


def git(root: Path, *args: str) -> str:
    return execute(["git", "-C", str(root), *args]).decode().strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_hardware(config: Config) -> dict[str, int | float | str]:
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise RuntimeError("Runner requires Linux x86_64")
    cpus: int | float = len(os.sched_getaffinity(0))
    quota = Path("/sys/fs/cgroup/cpu.max")
    if quota.exists():
        limit, period = quota.read_text().split()
        if limit != "max":
            cpus = min(cpus, int(limit) / int(period))
    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    memory_limit = Path("/sys/fs/cgroup/memory.max")
    if memory_limit.exists() and memory_limit.read_text().strip() != "max":
        memory = min(memory, int(memory_limit.read_text()))
    if config.go_cpus > cpus or memory < (config.go_memory_mib + 2048) * 1024**2:
        raise RuntimeError("Actual CPU/RAM allocation is too small for the selected settings")
    return {"effective_cpus": cpus, "memory_bytes": memory, "machine": platform.machine()}


def fetch_source(config: Config, directory: Path) -> None:
    source = directory / "source"
    source.mkdir(exist_ok=True)
    git(source, "init")
    execute(["git", "-C", str(source), "fetch", "--depth=1", config.source_repository, config.source_commit],
            timeout=600)
    git(source, "checkout", "--detach", "FETCH_HEAD")
    if git(source, "rev-parse", "HEAD") != config.source_commit:
        raise RuntimeError("Fetched leader commit differs from the pinned source")
    if not (source / "main.py").is_file():
        raise RuntimeError("Leader commit lacks main.py")


def verify_image() -> str:
    import witness_client

    metadata = json.loads(IMAGE_MANIFEST.read_bytes())
    source_commit = metadata["runtime_commit"]
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise RuntimeError("Generation image source commit is invalid")
    if metadata["purpose"] != "release":
        raise RuntimeError("Generation image is a development build")
    library = Path(witness_client.__file__).with_name("libwitnessproof.so")
    if digest(library) != metadata["library_sha256"]:
        raise RuntimeError("Installed prover differs from the image manifest")
    if any(digest(IMAGE_PARAMETERS / name) != expected for name, expected in PARAMETERS.items()):
        raise RuntimeError("Public proof parameters differ")
    return source_commit


def runtime_environment(config: Config) -> dict[str, str]:
    return {"WITNESS_PROVER_CONTEXTS": str(config.prover_contexts), "GOMAXPROCS": str(config.go_cpus),
            "GOMEMLIMIT": f"{config.go_memory_mib}MiB", "PYTHONUNBUFFERED": "1"}


def fingerprint(inputs: dict[str, object]) -> str:
    """Hash the RoundInputs wire representation using its protocol canonicalization."""
    data = {key: value for key, value in inputs.items() if key not in ("revision", "prompts")}
    rows = inputs["prompts"]
    if not isinstance(rows, list):
        raise TypeError("Invalid frozen prompts")
    data["prompts"] = [{key: row[key] for key in ("id", "sha256", "image_key")} for row in rows]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_context(config: Config, context: dict[str, object]) -> dict[str, str]:
    inputs = context.get("round_inputs")
    if not isinstance(inputs, dict) or not context.get("saving_enabled") or not inputs.get("save_submissions"):
        raise ValueError("Witness result saving is unavailable")
    if inputs.get("seed") != config.seed:
        raise ValueError("Witness seed differs from the job")
    rows = inputs["prompts"]
    if {row["id"]: row["sha256"] for row in rows} != config.prompts:
        raise ValueError("Witness prompts differ from the job")
    if config.round_inputs_fingerprint is not None and fingerprint(inputs) != config.round_inputs_fingerprint:
        raise ValueError("Witness frozen inputs differ from the job")
    return {row["id"]: row["source_url"] for row in rows}


def install_identity(config: Config, directory: Path, value: str) -> None:
    from bittensor_wallet import Wallet

    private = directory / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=private) as temporary:
        root = Path(temporary)
        path = root / "leader/hotkeys/default"
        path.parent.mkdir(parents=True)
        path.write_bytes(base64.b64decode(value, validate=True))
        path.chmod(0o600)
        if Wallet(name="leader", hotkey="default", path=str(root)).hotkey.ss58_address != config.hotkey:
            raise ValueError("Job hotkey differs from the frozen identity")
        destination = private / "wallets/leader/hotkeys/default"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError("Generation identity is already provisioned")
        shutil.copyfile(path, destination)
        destination.chmod(0o600)


def install_credentials(config: Config, directory: Path, access_token: str) -> None:
    from witness_client.request import load_credentials

    private = directory / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=private)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({"access_token": access_token, "account_id": config.account_id}, output)
        load_credentials({"witness_url": config.witness_url, "proof_parameters": "unused",
                          "codex_credentials_file": str(temporary)}, "codex", None)
        temporary.replace(private / "subscription.json")
    finally:
        temporary.unlink(missing_ok=True)


def load_miner(source: Path) -> ModuleType:
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location("main", source / "main.py")
    if spec is None or spec.loader is None:
        raise ImportError("Cannot import the pinned leader main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["main"] = module
    spec.loader.exec_module(module)
    return module


async def generate_pass(
    config: Config,
    witness: AsyncWitnessClient,
    generate: Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]],
) -> None:
    import httpx
    from miner_runtime.batch import run_pass

    async def validate_witness(client: AsyncWitnessClient) -> dict[str, str]:
        async with httpx.AsyncClient(transport=client.renderer_transport(), trust_env=False) as control:
            response = await control.get("https://witness.pod/v1/artifacts/context")
            response.raise_for_status()
            return check_context(config, response.json())

    await witness.replace_pod()
    urls = await validate_witness(witness)
    await run_pass(
        generate,
        dict(urls),
        config.seed,
        workers=config.workers,
        validate_witness=validate_witness,
        client=witness,
        prompt_hashes=config.prompts,
    )
    await validate_witness(witness)
    submitted = await witness.get_submitted()
    if set(submitted) != set(config.prompts):
        raise RuntimeError("Witness results do not cover exactly the official prompts")


async def generate_batch(config: Config, directory: Path) -> None:
    from bittensor_wallet import Wallet
    from witness_client import AsyncWitnessClient, WitnessConfig

    hotkey = Wallet(name="leader", hotkey="default", path=str(directory / "private/wallets")).hotkey
    if hotkey.ss58_address != config.hotkey:
        raise ValueError("Runner hotkey differs from the frozen identity")
    generate = load_miner(directory / "source").generate
    witness_config: WitnessConfig = {"witness_url": config.witness_url,
                                    "proof_parameters": str(IMAGE_PARAMETERS),
                                    "codex_credentials_file": str(directory / "private/subscription.json")}
    async with AsyncWitnessClient(
        witness_config, hotkey=hotkey, subscription="codex",
        parallel_calls=config.parallel_calls, parallel_preparations=config.parallel_preparations,
    ) as witness:
        requested_at: float | None = None
        while True:
            if requested_at is not None:
                delay = max(0, requested_at + 1200 - time.monotonic())
                if delay:
                    await asyncio.sleep(delay)
            requested_at = time.monotonic()
            try:
                await generate_pass(config, witness, generate)
            except Exception as error:
                print(f"Generation pass failed ({type(error).__name__}); pending prompts will be retried", flush=True)
            else:
                return


async def run_job(config: Config, directory: Path, *, hotkey: str, access_token: str) -> None:
    install_credentials(config, directory, access_token)
    install_identity(config, directory, hotkey)
    await generate_batch(config, directory)


def main() -> None:
    os.umask(0o077)
    config = Config(**json.loads(os.environ.pop("LEADER_JOB_CONFIG")))
    hotkey = os.environ.pop("LEADER_JOB_HOTKEY")
    access_token = os.environ.pop("LEADER_JOB_ACCESS_TOKEN")
    os.environ.update(runtime_environment(config))
    source_commit = verify_image()
    print(f"Generation image source commit: {source_commit}", flush=True)
    check_hardware(config)
    REMOTE.mkdir(parents=True, exist_ok=True)
    fetch_source(config, REMOTE)

    async def run() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=config.workers + config.parallel_calls))
        await run_job(config, REMOTE, hotkey=hotkey, access_token=access_token)

    asyncio.run(run())


if __name__ == "__main__":
    main()
