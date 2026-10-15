"""Benchmark provenance uses the copied sources and rejects checkout drift."""

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
        "commit": "a" * 40, "status": "", "on_build": lambda: None,
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
            return json.dumps({
                "length": 164, "cold": {"wall_seconds": 1},
                "proofs": [{"wall_seconds": 1}], "process": {"peak_rss_bytes": 1},
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
