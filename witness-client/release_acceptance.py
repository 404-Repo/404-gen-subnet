"""Explicit, billable release check of the two shipped witness examples.

Run with a non-editable Linux wheel installation. Evidence and child output stay
in a fresh private directory. Each case invokes the public CLI exactly once;
delivery failures never trigger a new generation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import witness_client
from witness_client import WitnessError
from witness_client.response import completed_response, provider_error

CASES = ("check-api", "check-subscription", "miner-api", "miner-subscription")
IMAGE = "https://sn12domain.org/procgen/45b19f215da1a99f434e4f3f2ad899f2057057aa1ef609a68bb0d5acf61e950c.png"
MAX_BYTES = 16 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def artifact_identity(wheel: Path, source_commit: str) -> dict[str, Any]:
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", source_commit)), "Use a full source commit"
    )
    require(sys.version_info >= (3, 11), "Python 3.11+ is required")
    libc, version = platform.libc_ver()
    require(
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and libc == "glibc"
        and tuple(map(int, version.split("."))) >= (2, 34),
        "Acceptance requires Linux x86_64 with glibc >= 2.34",
    )
    distribution = importlib.metadata.distribution("404-witness-client")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    require(
        not direct.get("dir_info", {}).get("editable"), "Editable installs cannot pass"
    )
    imported = Path(witness_client.__file__).resolve()
    require(
        imported.is_relative_to(Path(sys.prefix).resolve())
        and "site-packages" in imported.parts,
        "The imported client must be installed in this interpreter's site-packages",
    )
    with zipfile.ZipFile(wheel) as archive:
        metadata = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        require(len(metadata) == 1, "Expected one wheel distribution")
        parsed = BytesParser().parsebytes(archive.read(metadata[0]))
        require(parsed["Name"] == "404-witness-client", "Wrong wheel distribution")
        require(
            parsed["Version"] == distribution.version,
            "Installed and wheel versions differ",
        )
        require(
            any(
                name.endswith(".dist-info/WHEEL")
                and b"manylinux_2_34_x86_64" in archive.read(name)
                for name in archive.namelist()
            ),
            "Use the repaired manylinux release wheel",
        )
        package_files = [
            name
            for name in archive.namelist()
            if name.startswith(("witness_client/", "404_witness_client.libs/"))
            and not name.endswith("/")
        ]
        require(bool(package_files), "Wheel package files are missing")
        for name in package_files:
            require(
                Path(str(distribution.locate_file(name))).read_bytes()
                == archive.read(name),
                f"Installed wheel file differs: {name}",
            )
    scripts = Path(__file__).resolve().parent / "examples"
    return {
        "source_commit": source_commit,
        "distribution": distribution.metadata["Name"],
        "version": distribution.version,
        "wheel": str(wheel),
        "wheel_sha256": digest(wheel.read_bytes()),
        "imported_package": str(imported),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "glibc": version,
        "scripts": {
            name: digest((scripts / name).read_bytes())
            for name in ("check_witness.py", "miner.py")
        },
        "installed_distributions": {
            d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
        },
    }


def read_evidence(origin: str, prefix: str, destination: Path) -> dict[str, bytes]:
    objects: dict[str, bytes] = {}
    destination.mkdir(mode=0o700)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        for name in (
            "session.json",
            "status.json",
            "manifest.json",
            "request.json",
            "response.sse",
        ):
            with client.stream(
                "GET", f"{origin.rstrip('/')}/{prefix}/{name}"
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    require(len(raw) <= MAX_BYTES, "Evidence exceeds 16 MiB")
            objects[name] = bytes(raw)
            (destination / name).write_bytes(raw)
    return objects


def verify_case(
    case: str,
    directory: Path,
    origin: str,
    round_number: str,
    image: str,
    model: str,
    returncode: int,
) -> dict[str, Any]:
    states = list((directory / "state-home/404-witness-client").glob("*/job.json"))
    require(
        len(states) == 1, "Expected exactly one allocated exchange; inspect child logs"
    )
    state = states[0].parent
    session = json.loads((state / "session.json").read_bytes())
    job = json.loads(states[0].read_bytes())
    prefix = f"rounds/{round_number}/{session['miner']}/{state.name}"
    objects = read_evidence(origin, prefix, directory / "evidence")
    manifest, status = (
        json.loads(objects[name]) for name in ("manifest.json", "status.json")
    )
    require(
        session == json.loads(objects["session.json"]),
        "R2 session differs from local state",
    )
    require(
        session["round"] == round_number and session["id"] == state.name,
        "Wrong session identity",
    )
    require(
        manifest["prefix"] == prefix and manifest["exchange_id"] == state.name,
        "Wrong R2 prefix",
    )
    require(
        manifest["round"] == round_number and manifest["hotkey"] == session["miner"],
        "Wrong R2 identity",
    )
    subscription = case.endswith("subscription")
    provider = (
        "https://chatgpt.com/backend-api/codex/responses"
        if subscription
        else "https://api.openai.com/v1/responses"
    )
    require(
        session["provider"] == provider, "The case used the wrong authentication mode"
    )
    require(
        job["provider"] == ("codex" if subscription else "api"), "Wrong signed provider"
    )
    require(
        status["status"] == "issued" and status["authorized"] is True,
        "Evidence is not issued and authorized",
    )
    request = objects["request.json"]
    require(
        request == (state / "request.json").read_bytes(),
        "R2 request differs from client request",
    )
    require(digest(request) == manifest["request_sha256"], "R2 request hash mismatch")
    body = json.loads(request)
    require(
        body["stream"] is True and body["model"] == model,
        "Wrong public request settings",
    )
    if subscription:
        require(
            body["store"] is False and isinstance(body["input"], list),
            "Invalid subscription input",
        )
    response = objects["response.sse"]
    require(
        digest(response) == manifest["response_sha256"], "R2 response hash mismatch"
    )
    result: dict[str, Any] = {
        "exchange_id": state.name,
        "hotkey": session["miner"],
        "r2_prefix": prefix,
        "provider": provider,
        "provider_status": status["provider_status"],
        "request_sha256": digest(request),
        "response_sha256": digest(response),
        "usage": manifest.get("usage"),
    }
    error: WitnessError | None = None
    if status["provider_status"] != 200:
        error = provider_error(status["provider_status"], response)
    else:
        try:
            completed_response(response)
        except WitnessError as streaming_error:
            error = streaming_error
    if error is not None:
        result.update(
            status=(
                "blocked"
                if error.provider_code
                in {
                    "insufficient_quota",
                    "credit_balance_exhausted",
                    "organization_spend_limit_exceeded",
                    "project_spend_limit_exceeded",
                    "organization_usage_limit_exceeded",
                }
                else "failed"
            ),
            diagnostics={
                "source": error.source,
                "http_status": error.status_code,
                "code": error.code,
                "provider_code": error.provider_code,
                "param": error.param,
                "provider_message": error.provider_message,
            },
        )
        require(
            returncode != 0,
            "The public script incorrectly accepted a provider rejection",
        )
        return result
    require(
        response == (state / "response.bin").read_bytes(),
        "R2 response differs from delivered response",
    )
    events = [
        json.loads(line[5:])
        for line in response.splitlines()
        if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"
    ]
    completed = [
        event["response"]
        for event in events
        if event.get("type") == "response.completed"
    ]
    require(
        len(completed) == 1 and completed[0]["status"] == "completed",
        "Missing completed response",
    )
    answer = completed[0]
    usage = answer["usage"]
    require(
        all(
            type(usage[k]) is int and usage[k] >= 0
            for k in ("input_tokens", "output_tokens", "total_tokens")
        ),
        "Invalid usage",
    )
    require(
        usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"] > 0,
        "Invalid token total",
    )
    require(
        {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")}
        == manifest["usage"]
        == status["usage"],
        "R2 usage mismatch",
    )
    require(
        status["usage_state"] == "recorded" and not manifest["provider_rejected"],
        "Provider completion was rejected",
    )
    output = "".join(
        content["text"]
        for item in answer["output"]
        if item.get("type") == "message"
        for content in item["content"]
        if content.get("type") == "output_text"
    )
    stdout = (directory / "stdout.log").read_text()
    printed_usage = [
        json.loads(line.removeprefix("Usage: "))
        for line in stdout.splitlines()
        if line.startswith("Usage: ")
    ]
    require(printed_usage == [usage], "Public CLI token usage differs from evidence")
    if case.startswith("check"):
        require(
            output == "witness test OK" and output in stdout.splitlines(),
            "Wrong check_witness output",
        )
    else:
        output_path = directory / "results/001.js"
        submission = json.loads((directory / "results/001.json").read_bytes())
        require(
            bool(output.strip()) and output_path.read_text() == output,
            "Saved JavaScript differs from evidence",
        )
        require(
            not output.lstrip().startswith("```"),
            "Miner returned Markdown instead of JavaScript",
        )
        require(
            submission
            == {
                "hotkey": session["miner"],
                "image_url": image,
                "result": "001.js",
                "usage": usage,
            },
            "Miner metadata differs from evidence",
        )
        require(
            sorted(p.name for p in output_path.parent.iterdir())
            == ["001.js", "001.json"],
            "Unexpected miner artifacts",
        )
        result["javascript_sha256"] = digest(output_path.read_bytes())
    require(
        returncode == 0,
        "Public script exited unsuccessfully; inspect private child logs",
    )
    result.update(
        status="passed",
        usage=usage,
        output_bytes=len(output.encode()),
        evidence_matches=True,
    )
    return result


def accepted(cases: dict[str, Any]) -> bool:
    return set(cases) == set(CASES) and all(
        cases[name].get("status") == "passed" for name in CASES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--wallet-path", type=Path, required=True)
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", required=True)
    parser.add_argument("--proof-parameters", type=Path, required=True)
    parser.add_argument(
        "--witness-url", default="https://404-witness-public-test.subnet404.workers.dev"
    )
    parser.add_argument("--evidence-origin", default="https://subnet404.xyz")
    parser.add_argument("--round", default="test-0")
    parser.add_argument("--model", required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--timeout",
        type=float,
        default=1200,
        help="Per process deadline; never regenerates after timeout",
    )
    args = parser.parse_args()
    os.umask(0o077)
    require(args.timeout > 0, "Timeout must be positive")
    for url in (args.witness_url, args.evidence_origin, args.image):
        require(url.startswith("https://"), "Use HTTPS endpoints")
    output = args.output.expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "accepted": False,
        "cases": {name: {"status": "pending"} for name in CASES},
        "started_at": time.time(),
    }
    report_path = output / "acceptance.json"
    try:
        report["artifact"] = artifact_identity(args.wheel.resolve(), args.source_commit)
    except Exception as error:
        report["preflight_error"] = str(error)
        save_json(report_path, report)
        print(f"Release installation preflight failed; see {report_path}", flush=True)
        return 1
    save_json(report_path, report)
    scripts = Path(__file__).resolve().parent / "examples"
    wallet = [
        "--wallet-name",
        args.wallet_name,
        "--wallet-hotkey",
        args.wallet_hotkey,
        "--wallet-path",
        str(args.wallet_path.expanduser().resolve()),
        "--model",
        args.model,
    ]
    for name in CASES:
        directory = output / name
        directory.mkdir(mode=0o700)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "PYTHONHOME")
        }
        env["XDG_STATE_HOME"] = str(directory / "state-home")
        env["GOMAXPROCS"], env["GOMEMLIMIT"] = "2", "768MiB"
        auth = ["--subscription"] if name.endswith("subscription") else []
        if auth:
            env.pop("OPENAI_API_KEY", None)
        if name.startswith("check"):
            command = [
                sys.executable,
                str(scripts / "check_witness.py"),
                *wallet,
                *auth,
                "--witness-url",
                args.witness_url,
                "--proof-parameters",
                str(args.proof_parameters.resolve()),
                "--delivery-timeout",
                "120",
            ]
        else:
            config = directory / "config.json"
            save_json(
                config,
                {
                    "witness_url": args.witness_url,
                    "proof_parameters": str(args.proof_parameters.resolve()),
                },
            )
            command = [
                sys.executable,
                str(scripts / "miner.py"),
                args.image,
                *wallet,
                *auth,
                "--config",
                str(config),
                "--output",
                str(directory / "results"),
            ]
        case: dict[str, Any] = {
            "status": "running",
            "command": command,
            "started_at": time.time(),
        }
        report["cases"][name] = case
        save_json(report_path, report)
        print(f"Running {name} (one public-script invocation)", flush=True)
        try:
            with (directory / "stdout.log").open("wb") as stdout, (
                directory / "stderr.log"
            ).open("wb") as stderr:
                process = subprocess.run(
                    command,
                    cwd=directory,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=args.timeout,
                    check=False,
                )
            case["returncode"] = process.returncode
            case.update(
                verify_case(
                    name,
                    directory,
                    args.evidence_origin,
                    args.round,
                    args.image,
                    args.model,
                    process.returncode,
                )
            )
        except subprocess.TimeoutExpired:
            case.update(
                status="uncertain",
                error="Process timed out; inspect/recover this exchange before another generation",
            )
        except Exception as error:
            case.update(status="failed", error=str(error))
            allocated = list(
                (directory / "state-home/404-witness-client").glob("*/job.json")
            )
            if allocated and not (directory / "evidence/manifest.json").is_file():
                case.update(status="uncertain", exchange_id=allocated[0].parent.name)
            elif not auth and not env.get("OPENAI_API_KEY"):
                case.update(
                    status="blocked",
                    error="OPENAI_API_KEY is missing; see public-script stderr",
                )
        case["finished_at"] = time.time()
        save_json(report_path, report)
        print(
            f"{name}: {case['status']}; usage={json.dumps(case.get('usage'))}",
            flush=True,
        )
        if case["status"] == "uncertain":
            break  # Leave later cases pending; never replace an uncertain allocation.
    report.update(accepted=accepted(report["cases"]), finished_at=time.time())
    save_json(report_path, report)
    print(
        f"Release acceptance: {'PASSED' if report['accepted'] else 'FAILED'}; {report_path}",
        flush=True,
    )
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
