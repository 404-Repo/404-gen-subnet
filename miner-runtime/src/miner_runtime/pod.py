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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from urllib.parse import quote, urlsplit

IMAGE_PATTERN = r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
POD_NAME_PREFIX = "leader-runner-"
IMAGE_ROOT = Path("/opt/leader-image")
IMAGE_PARAMETERS = IMAGE_ROOT / "params"
IMAGE_MANIFEST = IMAGE_ROOT / "provenance.json"
REMOTE = Path("/opt/leader-run")
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
    competition_repository: str
    competition_branch: str
    round_number: int
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

    def __post_init__(self) -> None:
        if not re.fullmatch(IMAGE_PATTERN, self.generation_image):
            raise ValueError("Generation image must be pinned by SHA256 digest")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.competition_repository):
            raise ValueError("Competition repository must be a GitHub owner/repository")
        if not self.competition_branch or self.round_number < 0:
            raise ValueError("Expected a competition branch and round")
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{48}", self.hotkey):
            raise ValueError("Expected an operator hotkey address")
        if not re.fullmatch(r"[a-f0-9-]{1,80}", self.account_id):
            raise ValueError("Expected a subscription account identifier")
        https(self.witness_url)
        if urlsplit(self.witness_url).path not in ("", "/"):
            raise ValueError("Witness URL must be an HTTPS origin")
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
    name = name or f"{POD_NAME_PREFIX}{uuid.uuid4()}"
    if not re.fullmatch(rf"{POD_NAME_PREFIX}[A-Za-z0-9-]{{1,64}}", name):
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


def fetch_source(repo: str, commit: str, directory: Path) -> None:
    from subnet_common.witness.rounds import repository

    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Use a full 40-character source commit")
    url = f"https://github.com/{repository(repo)}"
    source = directory / "source"
    source.mkdir(exist_ok=True)
    git(source, "init")
    execute(["git", "-C", str(source), "fetch", "--depth=1", url, commit], timeout=600)
    git(source, "checkout", "--detach", "FETCH_HEAD")
    if git(source, "rev-parse", "HEAD") != commit:
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


def prompt_url(value: str, config: Config, revision: str) -> str:
    parsed = urlsplit(value)
    origin = f"https://raw.githubusercontent.com/{config.competition_repository}/{revision}/"
    if not parsed.scheme and not parsed.netloc:
        if value.startswith("/") or any(part in (".", "..") for part in value.split("/")):
            raise ValueError("Invalid repository prompt path")
        return origin + quote(value, safe="/")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
        raise ValueError("Prompt references must use HTTPS")
    repo_path = f"/{config.competition_repository}/"
    if parsed.path[:len(repo_path)].lower() == repo_path.lower():
        path = parsed.path[len(repo_path):]
        for host, prefix in (
            ("raw.githubusercontent.com", f"{config.competition_branch}/"),
            ("github.com", f"blob/{config.competition_branch}/"),
        ):
            if parsed.hostname == host and path.startswith(prefix):
                return origin + path.removeprefix(prefix)
    return value


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


async def generate_batch(config: Config, directory: Path, pending: dict[str, str | bytes], seed: int) -> None:
    from bittensor_wallet import Wallet
    from witness_client import AsyncWitnessClient, WitnessConfig, WitnessError

    from miner_runtime.batch import MAX_PROMPT_DOWNLOAD_BYTES, run_pass

    hotkey = Wallet(name="leader", hotkey="default", path=str(directory / "private/wallets")).hotkey
    if hotkey.ss58_address != config.hotkey:
        raise ValueError("Runner hotkey differs from the frozen identity")
    print("Loading pinned leader code", flush=True)
    generate = load_miner(directory / "source").generate
    witness_config: WitnessConfig = {"witness_url": config.witness_url,
                                    "proof_parameters": str(IMAGE_PARAMETERS),
                                    "codex_credentials_file": str(directory / "private/subscription.json")}
    print("Initializing witness client and native prover", flush=True)
    async with AsyncWitnessClient(
        witness_config, hotkey=hotkey, subscription="codex",
        parallel_calls=config.parallel_calls, parallel_preparations=config.parallel_preparations,
    ) as witness:
        requested_at: float | None = None
        while True:
            if requested_at is not None:
                delay = max(0, requested_at + 1200 - time.monotonic())
                if delay:
                    print(f"Retrying in {delay:.0f} seconds", flush=True)
                    await asyncio.sleep(delay)
            operation = "Witness pod request"
            print(f"Requesting witness pod from {config.witness_url}", flush=True)
            requested_at = time.monotonic()
            try:
                await witness.replace_pod()
                operation = "Generation pass"
                print("Witness pod ready. Reading saved prompt results", flush=True)
                await run_pass(
                    generate, pending, seed, workers=config.workers, client=witness,
                    max_prompt_bytes=MAX_PROMPT_DOWNLOAD_BYTES,
                )
            except Exception as error:
                reason = type(error).__name__
                if isinstance(error, WitnessError) and error.status_code is not None:
                    reason = f"HTTP {error.status_code}"
                print(f"{operation} failed ({reason}). Retry will check pending prompt IDs ({len(pending)}).", flush=True)
            else:
                print("Generation pass finished. Worker is exiting.", flush=True)
                return


async def run_job(config: Config, directory: Path, *, hotkey: str, access_token: str) -> None:
    from subnet_common.competition.leader import require_leader_state
    from subnet_common.competition.prompts import prompt_ids, require_prompts
    from subnet_common.competition.seed import require_seed_from_git
    from subnet_common.competition.state import RoundStage, require_state
    from subnet_common.github import GitHubClient

    async with GitHubClient(config.competition_repository) as git:
        revision = await git.get_ref_sha(config.competition_branch)
        state = await require_state(git, revision)
        if state.current_round != config.round_number or state.stage != RoundStage.MINER_GENERATION:
            print(f"Round {config.round_number} skipped: Git shows round {state.current_round} at {state.stage.value}",
                  flush=True)
            return
        leader = (await require_leader_state(git, revision)).get_latest()
        if leader is None:
            raise ValueError("Competition has no incumbent leader")
        seed = await require_seed_from_git(git, state.current_round, revision)
        urls = await require_prompts(git, state.current_round, revision)
    if not 0 <= seed < 2**64:
        raise ValueError("Seed must be an unsigned 64-bit integer")
    pending: dict[str, str | bytes] = {
        name: prompt_url(url, config, revision) for name, url in zip(prompt_ids(urls), urls, strict=True)
    }
    print(f"Round {state.current_round} at {revision[:10]}: {len(pending)} prompts, "
          f"leader {leader.repo}@{leader.commit}", flush=True)
    print("Fetching pinned leader source from GitHub", flush=True)
    await asyncio.to_thread(fetch_source, leader.repo, leader.commit, directory)
    print("Installing worker credentials and hotkey", flush=True)
    install_credentials(config, directory, access_token)
    install_identity(config, directory, hotkey)
    await generate_batch(config, directory, pending, seed)


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

    async def run() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=config.workers + config.parallel_calls))
        await run_job(config, REMOTE, hotkey=hotkey, access_token=access_token)

    asyncio.run(run())


if __name__ == "__main__":
    main()
