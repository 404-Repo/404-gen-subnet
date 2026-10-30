"""Reject untrusted setup files before the credential prover can use them."""

import shutil
from pathlib import Path

import pytest

from witness_client.proof import Prover

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"


@pytest.mark.parametrize("name", ["circuit.bin", "proving.bin", "verifying.bin"])
@pytest.mark.parametrize("change", ["tampered", "truncated", "extended", "missing"])
def test_rejects_changed_parameters(tmp_path: Path, name: str, change: str) -> None:
    for source in PARAMS.glob("*.bin"):
        shutil.copyfile(source, tmp_path / source.name)
    path = tmp_path / name
    if change == "missing":
        path.unlink()
    else:
        with path.open("r+b") as stream:
            if change == "tampered":
                first = stream.read(1)
                stream.seek(0)
                stream.write(bytes([first[0] ^ 1]))
            elif change == "truncated":
                stream.truncate(path.stat().st_size - 1)
            else:
                stream.seek(0, 2)
                stream.write(b"x")
    with pytest.raises(RuntimeError, match="Invalid proof parameters: " + name):
        Prover(tmp_path)
