"""Local synthetic C-ABI benchmark. No provider or gateway requests.

Run with the client's Python dependencies installed. Each case is a fresh
process; proofs within it reuse authenticated parameters. Builds stay in the
output directory, and the source checkout and its prebuilt library are untouched.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import statistics
import subprocess
import sys
import time


def digest(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def run(command: list[str], **kwargs: object) -> str:
    return subprocess.check_output(command, text=True, **kwargs).strip()


def process_stats() -> dict[str, float]:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "user_seconds": r.ru_utime,
        "system_seconds": r.ru_stime,
        "peak_rss_bytes": r.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
    }


def synthetic_input(length: int) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    # Public, synthetic credential text; fresh private key/nonce every proof.
    alphabet = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~+/="
    token = bytes(alphabet[i % len(alphabet)] for i in range(length))
    key, nonce = os.urandom(32), os.urandom(12)
    cipher = ChaCha20Poly1305(key)

    def seal(plain: bytes, sequence: int) -> str:
        record_nonce = (int.from_bytes(nonce, "big") ^ sequence).to_bytes(12, "big")
        header = b"\x17\x03\x03" + (len(plain) + 16).to_bytes(2, "big")
        return (header + cipher.encrypt(record_nonce, plain, header)).hex()

    return {
        "key": key.hex(), "nonce": nonce.hex(),
        "token_record": seal(token + b"\x17", 0),
        "update_record": seal(b"\x18\x00\x00\x01\x00\x16", 1),
    }


def worker(args: argparse.Namespace) -> None:
    os.nice(10)
    started = time.perf_counter()
    library = ctypes.CDLL(str(args.library))
    library_load = time.perf_counter() - started
    library.WitnessProofABI.restype = ctypes.c_int
    if library.WitnessProofABI() != 2:
        raise RuntimeError("unexpected proof ABI")
    for name, argtypes in {
        "WitnessProofInit": [ctypes.c_char_p],
        "WitnessProof": [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int],
    }.items():
        function = getattr(library, name)
        function.argtypes, function.restype = argtypes, ctypes.c_void_p
    library.WitnessProofFree.argtypes = [ctypes.c_void_p]
    library.WitnessProofFree.restype = None
    if hasattr(library, "WitnessBenchmarkSnapshot"):
        library.WitnessBenchmarkSnapshot.restype = ctypes.c_void_p

    def call(name: str, *values: object) -> dict:
        pointer = getattr(library, name)(*values)
        if not pointer:
            raise RuntimeError("empty native result")
        try:
            result = json.loads(ctypes.string_at(pointer))
        finally:
            library.WitnessProofFree(pointer)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def measure(name: str, *values: object) -> tuple[dict, dict]:
        snapshot = "WitnessBenchmarkSnapshot"
        before = call(snapshot) if hasattr(library, snapshot) else {}
        usage_before = process_stats()
        load_before = os.getloadavg()
        started_unix_ns = time.time_ns()
        started = time.perf_counter()
        result = call(name, *values)
        elapsed = time.perf_counter() - started
        usage_after = process_stats()
        after = call(snapshot) if hasattr(library, snapshot) else {}
        metrics = {
            "wall_seconds": elapsed,
            "started_unix_ns": started_unix_ns,
            "load_average_before": load_before, "load_average_after": os.getloadavg(),
            "user_seconds": usage_after["user_seconds"] - usage_before["user_seconds"],
            "system_seconds": usage_after["system_seconds"] - usage_before["system_seconds"],
            "peak_rss_bytes": usage_after["peak_rss_bytes"],
            "before": before, "after": after,
            "timings": result.get("timings"),
        }
        return result, metrics

    parameters = os.fsencode(args.parameters)
    _, cold = measure("WitnessProofInit", parameters)
    # Prove and verify fresh exchanges; never store private key/record/proof data.
    proofs = []
    for index in range(args.repeats):
        private = synthetic_input(args.length)
        raw = json.dumps(private).encode()
        result, measured = measure("WitnessProof", parameters, raw, len(raw))
        expected_count = 3 + (args.length + 1 + 255) // 256
        if len(result["bundle"]["proofs"]) != expected_count:
            raise RuntimeError("unexpected component proof count")
        public = {key: value for key, value in private.items() if key != "key"}
        public["bundle"] = result["bundle"]
        verified = subprocess.run(
            [str(args.verifier), "verify", str(args.parameters)],
            input=json.dumps(public), text=True, capture_output=True, check=True, timeout=120,
        )
        if json.loads(verified.stdout).get("verified") is not True:
            raise RuntimeError("unchanged verifier rejected proof")
        measured.update(iteration=index, proof_count=expected_count, verified=True)
        proofs.append(measured)
    print(json.dumps({
        "length": args.length, "library_load_seconds": library_load,
        "load_average": os.getloadavg(), "cold": cold, "proofs": proofs,
        "process": process_stats(),
    }), flush=True)


Setting = tuple[str, int | None, int | None]


def native_sources(native: Path) -> list[Path]:
    return sorted(p for p in native.iterdir() if p.name in ("go.mod", "go.sum") or
                  (p.suffix == ".go" and not p.name.endswith("_test.go")))


def snapshot_source(source: Path, build: Path) -> dict:
    files = native_sources(source / "witness-client/native")
    source_commit = run(["git", "rev-parse", "HEAD"], cwd=source)
    source_status = run(["git", "status", "--short"], cwd=source)
    source_sha256 = {p.name: digest(p) for p in files}
    build.mkdir()
    for file in files:
        shutil.copyfile(file, build / file.name)
    copied_sha256 = {p.name: digest(build / p.name) for p in files}
    if copied_sha256 != source_sha256:
        raise RuntimeError("source checkout changed while copying benchmark sources")
    return {"source_commit": source_commit, "source_status": source_status,
            "source_sha256": copied_sha256}


def check_source_snapshot(source: Path, provenance: dict) -> None:
    files = native_sources(source / "witness-client/native")
    if (run(["git", "rev-parse", "HEAD"], cwd=source) != provenance["source_commit"] or
            run(["git", "status", "--short"], cwd=source) != provenance["source_status"] or
            {p.name: digest(p) for p in files} != provenance["source_sha256"]):
        raise RuntimeError("source checkout changed while building benchmark")


def instrument_build(build: Path) -> None:
    metrics_file = Path(__file__).with_name("runtime_metrics.go")
    shutil.copyfile(metrics_file, build / "benchmark_metrics.go")
    file = build / "python.go"
    text = file.read_text()
    anchor = "\tstarted := time.Now()\n\t_, verifyingError := verifyingParameter.read(directory)"
    if text.count(anchor) != 1:
        raise RuntimeError("parameter loader changed; inspect benchmark injection")
    file.write_text(text.replace(anchor, "\tbenchmarkConfigure()\n" + anchor))


def build_benchmark(args: argparse.Namespace, output: Path,
                    environment: dict[str, str]) -> tuple[Path, Path, dict]:
    """Build inside an existing output directory; return artifacts and provenance."""
    output = output.resolve()
    source = args.source.resolve()
    build_environment = dict(environment, GOMAXPROCS="2", GOFLAGS="-p=2")
    build = output / "source"
    provenance = snapshot_source(source, build)
    suffix = "dylib" if sys.platform == "darwin" else "so"
    library = output / f"libwitnessproof.{suffix}"
    verifier = output / "verifier"
    # Build verifier before adding benchmark-only code. Its source is unchanged.
    subprocess.run([args.go, "build", "-trimpath", "-o", str(verifier), "."],
                   cwd=build, env=build_environment, check=True)
    if args.library is None:
        instrument_build(build)
        subprocess.run([args.go, "build", "-trimpath", "-buildmode=c-shared",
                        "-o", str(library), "."], cwd=build, env=build_environment, check=True)
    else:
        library = args.library.resolve()
        if args.settings != "default":
            raise ValueError("external library requires --settings default")
    check_source_snapshot(source, provenance)
    provenance["build_source_sha256"] = {p.name: digest(p) for p in sorted(build.iterdir())}
    return library, verifier, provenance


def benchmark_manifest(args: argparse.Namespace, library: Path, verifier: Path,
                       provenance: dict, environment: dict[str, str]) -> dict:
    def sysctl(name: str) -> str | None:
        if sys.platform != "darwin":
            return None
        return run(["sysctl", "-n", name])

    return {
        **provenance,
        "benchmark_sha256": digest(Path(__file__)),
        "library_sha256": digest(library), "verifier_sha256": digest(verifier),
        "external_library": str(library) if args.library else None,
        "go_build_info": run([args.go, "version", "-m", str(library)]),
        "go_version": run([args.go, "version"]), "python_version": sys.version,
        "runtime_environment": {name: environment.get(name) for name in
                                ("GOMAXPROCS", "GOMEMLIMIT", "GOGC", "GODEBUG")},
        "hardware": {"platform": platform.platform(), "machine": platform.machine(),
                     "cpu_count": os.cpu_count(), "model": sysctl("hw.model"),
                     "cpu_brand": sysctl("machdep.cpu.brand_string"),
                     "memory_bytes": sysctl("hw.memsize")},
        "parameter_sha256": {p.name: digest(p) for p in args.parameters.glob("*.bin")},
        "notes": ["Fresh process, warm filesystem cache; no OS cache purge.",
                  ("External library used without instrumentation or source patches." if args.library
                   else "Benchmark-only metrics and resource override precede parameter reads."),
                  "Peak RSS is the process high-water mark, including Python and initialization.",
                  "Every proof uses fresh random TLS keys, nonce and prover randomness.",
                  "All results verified by the unmodified verifier; no network calls."],
        "settings": args.settings, "lengths": args.lengths,
        "rounds": args.rounds, "warm_repeats": args.repeats, "seed": args.seed,
        "worker_niceness_increment": 10,
    }


def parse_settings(raw: str) -> list[Setting]:
    settings: list[Setting] = []
    for value in raw.split(","):
        if value == "default":
            settings.append((value, None, None))
        else:
            cpu, memory = map(int, value.split(":"))
            if cpu < 1 or cpu > (os.cpu_count() or 1) or memory < 128:
                raise ValueError("unsafe benchmark settings")
            settings.append((value, cpu, memory))
    return settings


def run_case(library: Path, verifier: Path, parameters: Path, length: int, repeats: int,
             environment: dict[str, str], cpus: int | None, memory: int | None) -> dict:
    child_env = dict(environment)
    for name in ("WITNESS_BENCH_CPUS", "WITNESS_BENCH_MEMORY"):
        child_env.pop(name, None)
    if cpus is not None:
        child_env["WITNESS_BENCH_CPUS"] = str(cpus)
        child_env["WITNESS_BENCH_MEMORY"] = str(memory * 1024**2)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker",
               "--library", str(library), "--verifier", str(verifier),
               "--parameters", str(parameters.resolve()),
               "--length", str(length), "--repeats", str(repeats)]
    return json.loads(run(command, env=child_env, timeout=900))


def run_cases(args: argparse.Namespace, output: Path, settings: list[Setting],
              library: Path, verifier: Path, environment: dict[str, str]) -> list[dict]:
    rows = []
    for round_index in range(args.rounds):
        cases = [(setting, length) for setting in settings for length in args.lengths]
        random.Random(args.seed + round_index).shuffle(cases)
        for (label, cpus, memory), length in cases:
            result = run_case(library, verifier, args.parameters, length, args.repeats,
                              environment, cpus, memory)
            row = {"round": round_index, "setting": label, **result}
            rows.append(row)
            with (output / "samples.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            median = statistics.median(p["wall_seconds"] for p in row["proofs"])
            print(f"round={round_index} setting={label} bytes={length} "
                  f"init={row['cold']['wall_seconds']:.3f}s warm={median:.3f}s "
                  f"peak={row['process']['peak_rss_bytes']/1024**2:.0f}MiB", flush=True)
    return rows


def summarize(rows: list[dict], settings: list[Setting], lengths: list[int],
              repeats: int) -> list[dict]:
    summary = []
    for label, _, _ in settings:
        for length in lengths:
            selected = [r for r in rows if r["setting"] == label and r["length"] == length]
            warm = [p["wall_seconds"] for r in selected for p in r["proofs"]]
            summary.append({
                "setting": label, "length": length,
                "cold_median_seconds": statistics.median(r["cold"]["wall_seconds"] for r in selected),
                "warm_median_seconds": statistics.median(warm),
                "first_proof_median_seconds": statistics.median(r["proofs"][0]["wall_seconds"] for r in selected),
                "repeat_proof_median_seconds": (statistics.median(
                    p["wall_seconds"] for r in selected for p in r["proofs"][1:]
                ) if repeats > 1 else None),
                "warm_min_seconds": min(warm), "warm_max_seconds": max(warm),
                "peak_rss_bytes": max(r["process"]["peak_rss_bytes"] for r in selected),
                "verified_proofs": len(warm),
            })
    return summary


def benchmark(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ, GOTOOLCHAIN="local", GOPROXY="off", GOSUMDB="off")
    environment.setdefault("GOCACHE", str(output / "go-cache"))
    library, verifier, provenance = build_benchmark(args, output, environment)
    manifest = benchmark_manifest(args, library, verifier, provenance, environment)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    settings = parse_settings(args.settings)
    rows = run_cases(args, output, settings, library, verifier, environment)
    summary = summarize(rows, settings, args.lengths, args.repeats)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--go", default="go")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--settings", default="2:768,2:2048,2:4096,4:768,4:2048,4:4096,8:768,8:2048,8:4096")
    parser.add_argument("--lengths", type=int, nargs="+", default=[164, 1522])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--seed", type=int, default=404)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--length", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--verifier", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.parameters is None:
        args.parameters = args.source / "witness-server/verifier/params"
    if args.repeats < 1 or args.rounds < 1:
        parser.error("rounds and repeats must be positive")
    if any(length < 64 or length > 4096 for length in args.lengths):
        parser.error("credential lengths must be between 64 and 4096 bytes")
    if args.worker:
        worker(args)
    else:
        if args.output is None:
            parser.error("--output must be a new directory")
        benchmark(args)


if __name__ == "__main__":
    main()
