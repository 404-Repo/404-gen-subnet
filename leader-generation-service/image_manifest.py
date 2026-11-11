"""Verify installed generation SDK artifacts and record their image provenance."""

import importlib.metadata
import json
import sys
import tomllib
from pathlib import Path

import witness_client
from miner_runtime.pod import PARAMETERS, digest
from witness_client.proof import Prover


def record(source: Path, metadata_path: Path, parameters: Path, output: Path) -> None:
    metadata = json.loads(metadata_path.read_bytes())
    versions = {}
    for package in ("witness-client", "renderer-client", "miner-runtime"):
        project = tomllib.loads((source / package / "pyproject.toml").read_text())["project"]
        installed = importlib.metadata.version(project["name"])
        if installed != project["version"]:
            raise RuntimeError("Installed SDK version differs from the image source")
        versions[project["name"]] = installed
    if any(digest(parameters / name) != expected for name, expected in PARAMETERS.items()):
        raise RuntimeError("Public proof parameters differ")
    library = Path(witness_client.__file__).with_name("libwitnessproof.so")
    library_hash = digest(library)
    if library_hash != digest(source / "witness-client/src/witness_client/libwitnessproof.so"):
        raise RuntimeError("Installed prover differs from the newly built Linux library")
    if Prover(parameters).verifying_key_sha256 != PARAMETERS["verifying.bin"]:
        raise RuntimeError("Installed proof key differs")
    wheels = source / "wheels"
    wheel_files = list(wheels.glob("*.whl"))
    if len(wheel_files) != 3:
        raise RuntimeError("Expected witness, renderer and miner-runtime wheels from the image source")
    metadata.update(
        packages=versions,
        library_sha256=library_hash,
        parameters=PARAMETERS,
        wheels={path.name: digest(path) for path in wheel_files},
        go_build=(source / "go-build.txt").read_text(),
    )
    output.write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    record(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
