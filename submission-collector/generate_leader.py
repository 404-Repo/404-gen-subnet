"""Freeze, run and hand off one round's incumbent Python generation manually."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tarfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit


BASE_REPO = "404-Repo/404-base-miner-gpt"
BASE_COMMIT = "26c3ad32f9c4f202c9cd896c955695ab1964b75c"
DEFAULT_COMPETITION = "404-Repo/404-active-competition"


def git(directory: Path, *args: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required")
    return subprocess.check_output([executable, "-C", str(directory), *args])  # noqa: S603  # nosec B603


def repository(remote: str) -> str:
    match = re.fullmatch(
        r"(?:https://github.com/|git@github.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        remote.strip(),
    )
    if match is None:
        raise ValueError("Expected a GitHub origin")
    return match[1]


def check_repository(directory: Path, expected: str) -> None:
    if repository(git(directory, "remote", "get-url", "origin").decode()).lower() != expected.lower():
        raise ValueError(f"Checkout origin must be {expected}")


def read_json(directory: Path, ref: str, name: str) -> Any:
    return json.loads(git(directory, "show", f"{ref}:{name}"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def freeze_source(source: Path, commit: str, target: Path) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Source must use a full immutable Git commit")
    archive = git(source, "archive", "--format=tar", commit)
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for item in tar.getmembers():
            path = target / item.name
            if not path.resolve().is_relative_to(target.resolve()):
                raise ValueError("Source archive escapes its directory")
            if item.isdir():
                continue
            if not item.isfile():
                raise ValueError("Manual source snapshots require regular files; no submodules or symlinks")
            stream = tar.extractfile(item)
            if stream is None:
                raise ValueError("Missing source archive member")
            data = stream.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            hashes[item.name] = sha(data)
    if "main.py" not in hashes:
        raise ValueError("Frozen Python source requires main.py")
    return hashes


def round_inputs(competition: Path, ref: str, number: int) -> tuple[dict[str, Any], int, list[str]]:
    config = read_json(competition, ref, "config.json")
    seed = read_json(competition, ref, f"rounds/{number}/seed.json")["seed"]
    urls = git(competition, "show", f"{ref}:rounds/{number}/prompts.txt").decode().splitlines()
    urls = [url.strip() for url in urls if url.strip()]
    ids = [Path(urlsplit(url).path).stem for url in urls]
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("Invalid official seed")
    if len(urls) != config["prompts_per_round"] or len(ids) != len(set(ids)) or not ids:
        raise ValueError("Official prompt set is empty, incomplete or has duplicate IDs")
    if any(urlsplit(url).scheme != "https" for url in urls) or any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", key) is None for key in ids
    ):
        raise ValueError("Expected official HTTPS image URLs with distinct prompt filenames")
    return config, seed, urls


async def prepare(args: argparse.Namespace) -> None:
    import httpx

    check_repository(args.competition, args.competition_repo)
    ref = git(args.competition, "rev-parse", f"{args.ref}^{{commit}}").decode().strip()
    state = read_json(args.competition, ref, "state.json")
    if state["current_round"] != args.round:
        raise ValueError("Requested round is not the competition's current round")
    config, seed, urls = round_inputs(args.competition, ref, args.round)
    leader = read_json(args.competition, ref, "leader.json")[-1]
    check_repository(args.source, leader["repo"])
    check_repository(args.runner_source, BASE_REPO)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    hashes = freeze_source(args.source, leader["commit"], args.run_dir / "source")
    runner = git(args.runner_source, "show", f"{BASE_COMMIT}:miner/run.py")
    (args.run_dir / "runner.py").write_bytes(runner)
    (args.run_dir / "prompts").mkdir()
    prompts = {}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False) as http:
        for url in urls:
            prompt = Path(urlsplit(url).path).stem
            response = await http.get(url)
            response.raise_for_status()
            if not response.content or len(response.content) > 8 * 1024 * 1024:
                raise ValueError("Official prompt image is empty or exceeds 8 MiB")
            (args.run_dir / "prompts" / prompt).write_bytes(response.content)
            prompts[prompt] = {"url": url, "sha256": sha(response.content)}
    if len({row["sha256"] for row in prompts.values()}) != len(prompts):
        raise ValueError("Official prompt images must have distinct hashes")
    manifest = {
        "competition_repo": args.competition_repo,
        "competition_ref": ref,
        "round": args.round,
        "round_name": f"{config['name']}-{args.round}",
        "seed": seed,
        "leader": {key: leader[key] for key in ("repo", "commit", "hotkey")},
        "runner_repo": BASE_REPO,
        "runner_commit": BASE_COMMIT,
        "runner_sha256": sha(runner),
        "source_files": hashes,
        "prompts": prompts,
    }
    save(args.run_dir / "manifest.json", manifest)
    print(
        f"Frozen {len(prompts)} prompts, seed {seed}, {leader['repo']}@{leader['commit']}",
        flush=True,
    )


def load_manifest(root: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((root / "manifest.json").read_bytes())
    for name, digest in manifest["source_files"].items():
        if sha((root / "source" / name).read_bytes()) != digest:
            raise ValueError(f"Frozen source changed: {name}")
    actual = {str(p.relative_to(root / "source")) for p in (root / "source").rglob("*") if p.is_file()}
    if actual != set(manifest["source_files"]):
        raise ValueError("Frozen source contains extra files")
    if sha((root / "runner.py").read_bytes()) != manifest["runner_sha256"]:
        raise ValueError("Frozen runner changed")
    for name, row in manifest["prompts"].items():
        if sha((root / "prompts" / name).read_bytes()) != row["sha256"]:
            raise ValueError(f"Frozen image changed: {name}")
    return manifest


def results(manifest: dict[str, Any], hotkey: str, rows: Any, *, complete: bool = False) -> dict[str, Any]:
    from subnet_common.competition.generations import GenerationsAdapter

    parsed = GenerationsAdapter.validate_python(rows)
    expected = set(manifest["prompts"])
    if set(parsed) - expected or (complete and set(parsed) != expected):
        raise ValueError(f"Saved index does not match official prompts ({len(parsed)}/{len(expected)})")
    for prompt, row in parsed.items():
        prefix = f"rounds/{manifest['round_name']}/{hotkey}/submitted/{prompt}"
        if row.js is not None and (row.js != prefix + ".js" or row.size <= 0):
            raise ValueError("Saved JavaScript identity differs from the authenticated round/hotkey")
        if row.views is not None and row.views != prefix:
            raise ValueError("Saved views identity differs from the authenticated round/hotkey")
        if not row.failure_reason and (row.js is None or row.views is None):
            raise ValueError("Saved result is unfinished")
    return {prompt: row.model_dump() for prompt, row in parsed.items()}


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run(args: argparse.Namespace) -> None:
    import httpx
    from bittensor_wallet import Wallet
    from openai.types.responses import Response
    from witness_client import AsyncWitnessClient, WitnessError
    from witness_client.async_pods import AsyncPodGatewayClient
    from witness_client.client import _submission_body, _submission_result
    from witness_client.response import model_result

    root = args.run_dir
    manifest = load_manifest(root)
    config = json.loads(args.witness_config.read_bytes())
    hotkey = Wallet(
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=str(args.wallet_path.expanduser()),
    ).hotkey
    identity = {
        "hotkey": hotkey.ss58_address,
        "witness_url": config["witness_url"].rstrip("/"),
        "python": sys.version.split()[0],
        "packages": {
            name: importlib.metadata.version(name) for name in ("404-witness-client", "404-renderer-client", "openai")
        },
    }
    if identity["witness_url"] != "https://witness.subnet404.xyz":
        raise ValueError("The production leader must use https://witness.subnet404.xyz")
    if not Path(config["proof_parameters"]).expanduser().is_absolute():
        raise ValueError("Use an absolute proof_parameters path in witness.json")
    identity_file = root / "identity.json"
    if identity_file.exists() and json.loads(identity_file.read_bytes()) != identity:
        raise ValueError("Resume requires the same hotkey, witness origin, Python and client versions")
    save(identity_file, identity)
    usage_file = root / "usage.json"
    usage = json.loads(usage_file.read_bytes()) if usage_file.exists() else {}

    class RoundGateway(AsyncPodGatewayClient):
        async def _allocation_json(self, method: str, path: str, value: Any = None, *, deadline: float) -> Any:
            reply = await super()._allocation_json(method, path, value, deadline=deadline)
            if reply.get("round") != manifest["round_name"]:
                raise WitnessError(
                    "Gateway round differs from frozen official round",
                    code="round_mismatch",
                )
            return reply

    class LeaderWitness(AsyncWitnessClient):
        _gateway: AsyncPodGatewayClient

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # One preparation slot serializes the SDK's subscription login load/refresh.
            super().__init__(*args, **kwargs, parallel_preparations=1)

        async def __aenter__(self) -> Any:
            await self._gateway.aclose()
            self._gateway = RoundGateway(self._http, self._hotkey, self._state, self._gateway.delivery_timeout)
            await super().__aenter__()
            try:
                request = self._http.build_request("GET", "/v1/artifacts/context")
                response = await self._gateway.send(request)
                response.raise_for_status()
                commit = response.json()["submission_commit"]
                await response.aclose()
                if commit is not None and commit != manifest["leader"]["commit"]:
                    raise ValueError("Authenticated Pod has a different submitted source commit")
            except BaseException:
                await self.aclose()
                raise
            return self

        async def _resolve(self, key: Any, body: bytes, session_id: str | None) -> Any:
            pending = "unresolved:" + key.id
            usage[pending] = {
                "call_id": key.id,
                "request_id": key.request_id,
                "model": json.loads(body)["model"],
                "usage": None,
            }
            save(usage_file, usage)
            captured = await super()._resolve(key, body, session_id)
            record = {
                "call_id": key.id,
                "request_id": key.request_id,
                "status": captured.status,
                "model": json.loads(body)["model"],
                "usage": None,
            }
            try:
                response = cast(Response, model_result(captured.status, captured.body, "codex", stream=False))
                if response.usage is not None:
                    record["usage"] = response.usage.model_dump()
            finally:
                usage.pop(pending, None)
                usage[captured.exchange_id] = record
                save(usage_file, usage)
                tokens = [row["usage"] for row in usage.values() if row["usage"] is not None]
                print(
                    f"Usage observed: {sum(not key.startswith('unresolved:') for key in usage)} distinct captures, "
                    f"{sum(row['input_tokens'] for row in tokens)} input / "
                    f"{sum(row['output_tokens'] for row in tokens)} output tokens; "
                    f"{len(usage) - len(tokens)} without observed usage",
                    flush=True,
                )
            return captured

        async def get_submitted(self) -> Any:
            rows = results(manifest, identity["hotkey"], await super().get_submitted())
            save(
                root / "progress.json",
                {
                    "submitted": len(rows),
                    "expected": len(manifest["prompts"]),
                    "failures": sum(bool(row["failure_reason"]) for row in rows.values()),
                },
            )
            print(f"Saved results: {len(rows)}/{len(manifest['prompts'])}", flush=True)
            return rows

        async def submit(self, image: bytes, source: str | None) -> Any:
            body = _submission_body(image, source)
            generation = cast(dict[str, object], body["generation"])
            generation["seed"] = manifest["seed"]
            request = self._http.build_request("POST", "/v1/submissions", json=body, timeout=150)
            response = await self._gateway.send(request)
            try:
                result = _submission_result(response)
            finally:
                await response.aclose()
            await self.get_submitted()
            return result

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "source"))
    entry = load_module("_leader_entry", root / "source" / "main.py")
    if not asyncio.iscoroutinefunction(entry.generate):
        raise ValueError("Manual runner requires async generate(image, seed, witness)")
    inspect.signature(entry.generate).bind(b"", manifest["seed"], None)
    runner = load_module("_leader_runner", root / "runner.py")
    runner.AsyncWitnessClient = LeaderWitness
    runner.ROOT = args.witness_config.parent
    if args.witness_config.name != "witness.json":
        raise ValueError("The base runner reads a config named witness.json")
    await runner.run(
        entry.generate,
        {name: str(root / "prompts" / name) for name in manifest["prompts"]},
        manifest["seed"],
        workers=args.parallel_prompts,
        parallel_calls=args.parallel_calls,
        attempts=args.attempts,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        wallet_path=str(args.wallet_path.expanduser()),
    )
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        response = await http.get(f"{args.cdn_url}/rounds/{manifest['round_name']}/{identity['hotkey']}/submitted.json")
        response.raise_for_status()
        rows = results(manifest, identity["hotkey"], response.json(), complete=True)
    print(f"All {len(rows)} prompt outcomes saved; run handoff to verify artifacts and prepare the judge index.")


async def handoff(args: argparse.Namespace) -> None:
    import httpx
    from subnet_common.render import GRAY_VIEWS, WHITE_VIEWS

    root = args.run_dir
    manifest = load_manifest(root)
    identity = json.loads((root / "identity.json").read_bytes())
    check_repository(args.competition, manifest["competition_repo"])
    ref = git(args.competition, "rev-parse", "HEAD").decode().strip()
    config, seed, urls = round_inputs(args.competition, ref, manifest["round"])
    leader = read_json(args.competition, ref, "leader.json")[-1]
    if (
        seed != manifest["seed"]
        or urls != [row["url"] for row in manifest["prompts"].values()]
        or f"{config['name']}-{manifest['round']}" != manifest["round_name"]
        or any(leader[key] != manifest["leader"][key] for key in manifest["leader"])
    ):
        raise ValueError("Competition inputs or incumbent identity changed after freezing")
    index_url = f"{args.cdn_url}/rounds/{manifest['round_name']}/{identity['hotkey']}/submitted.json"
    async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
        response = await http.get(index_url)
        response.raise_for_status()
        rows = results(manifest, identity["hotkey"], response.json(), complete=True)
        slots = asyncio.Semaphore(8)

        async def require_object(key: str) -> None:
            async with slots:
                artifact = await http.head(f"{args.cdn_url}/{key}")
                artifact.raise_for_status()

        objects = []
        for row in rows.values():
            if row["js"]:
                objects.append(row["js"])
            if row["views"]:
                objects.extend(f"{row['views']}/white/{view.name}.png" for view in WHITE_VIEWS)
                objects.extend(f"{row['views']}/gray/{view.name}.png" for view in GRAY_VIEWS)
                objects.extend((f"{row['views']}/grid.png", f"{row['views']}/embeddings.npz"))
        if not any(row["views"] and not row["failure_reason"] for row in rows.values()):
            raise ValueError("Baseline has no usable rendered output")
        await asyncio.gather(*(require_object(key) for key in objects))
    for row in rows.values():
        for key in ("js", "views"):
            if row[key] is not None:
                row[key] = f"{args.cdn_url}/{row[key]}"
    destination = args.competition / "rounds" / str(manifest["round"]) / "leader"
    provenance = {
        key: manifest[key]
        for key in (
            "competition_repo",
            "competition_ref",
            "round",
            "round_name",
            "seed",
            "leader",
            "runner_repo",
            "runner_commit",
        )
    }
    provenance.update(
        witness=identity,
        submitted_index=index_url,
        submitted_index_sha256=sha(response.content),
        source_sha256=sha(json.dumps(manifest["source_files"], sort_keys=True).encode()),
    )
    files = {"source.json": provenance, "generated.json": rows}
    for name, value in files.items():
        path = destination / name
        if path.exists() and json.loads(path.read_bytes()) != value:
            raise ValueError(f"Refusing to overwrite a different {path}")
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        save(destination / name, value)
    print(f"Verified {len(rows)} terminal outcomes and {len(objects)} artifacts. Prepared {destination}/generated.json")
    print("Commit and push only leader/generated.json and leader/source.json to hand off to the judge.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser(
        "prepare",
        help="Freeze committed incumbent source and official published inputs",
    )
    freeze.add_argument("--competition", type=Path, required=True)
    freeze.add_argument("--competition-repo", default=DEFAULT_COMPETITION)
    freeze.add_argument("--ref", default="HEAD")
    freeze.add_argument("--round", type=int, required=True)
    freeze.add_argument("--source", type=Path, required=True)
    freeze.add_argument("--runner-source", type=Path, required=True)
    launch = commands.add_parser(
        "run",
        help="Run or resume the frozen source through the registered hotkey's Pod",
    )
    launch.add_argument("--witness-config", type=Path, required=True)
    launch.add_argument("--wallet-name", default="miner")
    launch.add_argument("--wallet-hotkey", default="default")
    launch.add_argument("--wallet-path", type=Path, default=Path("~/.bittensor/wallets"))
    launch.add_argument("--parallel-prompts", type=int, default=8)
    launch.add_argument("--parallel-calls", type=int, default=12)
    launch.add_argument("--attempts", type=int, default=3)
    publish = commands.add_parser(
        "handoff",
        help="Verify the saved index and artifacts, then write the local judge bundle",
    )
    publish.add_argument("--competition", type=Path, required=True)
    for command in (freeze, launch, publish):
        command.add_argument("--run-dir", type=Path, required=True)
    for command in (launch, publish):
        command.add_argument("--cdn-url", default="https://subnet404.xyz")
    args = parser.parse_args()
    args.run_dir = args.run_dir.expanduser().resolve()
    if args.command == "prepare":
        args.competition = args.competition.expanduser().resolve()
        asyncio.run(prepare(args))
        return
    args.cdn_url = args.cdn_url.rstrip("/")
    with (args.run_dir / "invocation.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "run":
            args.witness_config = args.witness_config.expanduser().resolve()
            if min(args.attempts, args.parallel_prompts, args.parallel_calls) < 1:
                parser.error("Attempts and concurrency must be positive")
            asyncio.run(run(args))
        else:
            args.competition = args.competition.expanduser().resolve()
            asyncio.run(handoff(args))


if __name__ == "__main__":
    os.umask(0o077)
    # Shared result contracts are used directly from this checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
    main()
