"""Benchmark provenance, instrumentation and result compatibility stay stable."""

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def benchmark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(
        "prover_benchmark", Path(__file__).resolve().parents[1] / "benchmarks/prover.py"
    )
    assert spec is not None and spec.loader is not None
    prover = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prover)
    source = tmp_path / "checkout"
    native = source / "witness-client/native"
    native.mkdir(parents=True)
    (native / "go.mod").write_text("module benchmark\n")
    (native / "main.go").write_text("package main\n")
    (native / "python.go").write_text(
        "\tstarted := time.Now()\n\t_, verifyingError := verifyingParameter.read(directory)\n"
    )
    parameters = tmp_path / "params"
    parameters.mkdir()
    state: dict[str, Any] = {
        "commit": "a" * 40, "status": "", "on_build": lambda: None, "cases": [],
    }

    def build(command: list[str], **kwargs: Any) -> None:
        artifact = Path(command[command.index("-o") + 1])
        artifact.write_bytes(b"synthetic build")
        if artifact.name == "verifier":
            state["on_build"]()

    def run(command: list[str], **kwargs: Any) -> str:
        if command == ["git", "rev-parse", "HEAD"]:
            return str(state["commit"])
        if command == ["git", "status", "--short"]:
            return str(state["status"])
        if "--worker" in command:
            length = int(command[command.index("--length") + 1])
            repeats = int(command[command.index("--repeats") + 1])
            environment = kwargs["env"]
            state["cases"].append((
                length, environment.get("WITNESS_BENCH_CPUS"),
                environment.get("WITNESS_BENCH_MEMORY"),
                environment.get("GOMAXPROCS"), environment.get("GOMEMLIMIT"),
            ))
            return json.dumps({
                "length": length, "cold": {"wall_seconds": 1},
                "proofs": [{"wall_seconds": i + 1} for i in range(repeats)],
                "process": {"peak_rss_bytes": 1},
            })
        return "synthetic metadata"

    monkeypatch.setattr(prover.subprocess, "run", build)
    monkeypatch.setattr(prover, "run", run)
    monkeypatch.setattr(prover.platform, "platform", lambda: "test platform")
    args = argparse.Namespace(
        source=source, output=tmp_path / "results", parameters=parameters,
        go="go", library=None, settings="default", lengths=[164],
        rounds=1, repeats=1, seed=404,
    )
    return {"prover": prover, "args": args, "native": native, "state": state}


@pytest.mark.parametrize("status", ["", " M witness-client/native/main.go"])
@pytest.mark.parametrize("external", [False, True])
def test_records_copied_source_provenance(
    benchmark: dict[str, Any], status: str, external: bool
) -> None:
    prover, args = benchmark["prover"], benchmark["args"]
    benchmark["state"]["status"] = status
    expected = {p.name: prover.digest(p) for p in benchmark["native"].iterdir()}
    if external:
        args.library = args.output.parent / "external.dylib"
        args.library.write_bytes(b"external library")
    prover.benchmark(args)
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert manifest["source_commit"] == "a" * 40
    assert manifest["source_status"] == status
    assert manifest["source_sha256"] == expected
    assert manifest["build_source_sha256"]["main.go"] == expected["main.go"]
    assert (manifest["build_source_sha256"]["python.go"] == expected["python.go"]) == external
    assert (args.output / "summary.json").is_file()


@pytest.mark.parametrize("change", ["edit", "add", "remove", "commit", "status"])
def test_rejects_checkout_changes_during_build(
    benchmark: dict[str, Any], change: str
) -> None:
    native, state = benchmark["native"], benchmark["state"]

    def change_source() -> None:
        if change == "edit":
            (native / "main.go").write_text("package changed\n")
        elif change == "add":
            (native / "new.go").write_text("package main\n")
        elif change == "remove":
            (native / "main.go").unlink()
        elif change == "commit":
            state["commit"] = "b" * 40
        else:
            state["status"] = " M other-file"

    state["on_build"] = change_source
    with pytest.raises(RuntimeError, match="source.*changed"):
        benchmark["prover"].benchmark(benchmark["args"])
    assert not (benchmark["args"].output / "manifest.json").exists()
    assert not (benchmark["args"].output / "samples.jsonl").exists()


def test_case_order_environment_and_summary(
    benchmark: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WITNESS_BENCH_CPUS", "99")
    monkeypatch.setenv("WITNESS_BENCH_MEMORY", "1")
    monkeypatch.setenv("GOMAXPROCS", "6")
    monkeypatch.setenv("GOMEMLIMIT", "2GiB")
    args = benchmark["args"]
    args.settings, args.lengths, args.rounds, args.repeats = "2:768,default", [164, 1522], 2, 3
    benchmark["prover"].benchmark(args)
    rows = [json.loads(line) for line in (args.output / "samples.jsonl").read_text().splitlines()]
    expected_order = [
        (0, "default", 164), (0, "default", 1522), (0, "2:768", 1522), (0, "2:768", 164),
        (1, "default", 164), (1, "2:768", 1522), (1, "default", 1522), (1, "2:768", 164),
    ]
    assert [(row["round"], row["setting"], row["length"]) for row in rows] == expected_order
    assert benchmark["state"]["cases"] == [
        (length, "2" if setting == "2:768" else None,
         str(768 * 1024**2) if setting == "2:768" else None, "6", "2GiB")
        for _, setting, length in expected_order
    ]
    assert json.loads((args.output / "summary.json").read_text()) == [
        {
            "setting": setting, "length": length, "cold_median_seconds": 1,
            "warm_median_seconds": 2, "first_proof_median_seconds": 1,
            "repeat_proof_median_seconds": 2.5, "warm_min_seconds": 1,
            "warm_max_seconds": 3, "peak_rss_bytes": 1, "verified_proofs": 6,
        }
        for setting in ("2:768", "default") for length in (164, 1522)
    ]


def test_rejects_source_changes_while_copying(
    benchmark: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prover = benchmark["prover"]
    copyfile = prover.shutil.copyfile

    def copy_changed_source(source: Path, destination: Path) -> None:
        if source.name == "main.go":
            source.write_text("package changed\n")
        copyfile(source, destination)

    monkeypatch.setattr(prover.shutil, "copyfile", copy_changed_source)
    with pytest.raises(RuntimeError, match="source.*changed"):
        prover.benchmark(benchmark["args"])
    assert not (benchmark["args"].output / "verifier").exists()
    assert not (benchmark["args"].output / "manifest.json").exists()


def test_instruments_only_the_copied_parameter_loader(
    benchmark: dict[str, Any], tmp_path: Path
) -> None:
    prover = benchmark["prover"]
    source = benchmark["native"] / "python.go"
    original = source.read_text()
    build = tmp_path / "instrumented"
    build.mkdir()
    copied = build / "python.go"
    copied.write_text(original)
    prover.instrument_build(build)
    assert copied.read_text() == "\tbenchmarkConfigure()\n" + original
    assert source.read_text() == original
    assert (build / "benchmark_metrics.go").read_bytes() == (
        Path(prover.__file__).with_name("runtime_metrics.go").read_bytes()
    )


@pytest.mark.parametrize("anchor_count", [0, 2])
def test_rejects_missing_or_ambiguous_instrumentation_anchor(
    benchmark: dict[str, Any], tmp_path: Path, anchor_count: int
) -> None:
    original = (benchmark["native"] / "python.go").read_text() * anchor_count
    build = tmp_path / "instrumented"
    build.mkdir()
    copied = build / "python.go"
    copied.write_text(original)
    with pytest.raises(RuntimeError, match="parameter loader changed"):
        benchmark["prover"].instrument_build(build)
    assert copied.read_text() == original


def test_reuses_case_runner_and_summary_for_multiple_libraries(
    benchmark: dict[str, Any], tmp_path: Path
) -> None:
    prover, args = benchmark["prover"], benchmark["args"]
    environment = {"GOMAXPROCS": "2", "WITNESS_BENCH_CPUS": "99"}
    rows = []
    for label in ("before", "after"):
        result = prover.run_case(
            tmp_path / label, tmp_path / "verifier", args.parameters, 164, 2,
            environment, None, None,
        )
        rows.append({"setting": label, **result})
    summary = prover.summarize(
        rows, [("before", None, None), ("after", None, None)], [164], 2
    )
    assert [row["setting"] for row in summary] == ["before", "after"]
    assert all(row["verified_proofs"] == 2 for row in summary)
    assert all(row["repeat_proof_median_seconds"] == 2 for row in summary)
    assert environment == {"GOMAXPROCS": "2", "WITNESS_BENCH_CPUS": "99"}
    assert not args.output.exists()
