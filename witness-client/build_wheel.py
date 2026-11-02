"""Build the native proof library and Python wheel."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
extension = "dylib" if sys.platform == "darwin" else "so"
if sys.platform not in ("linux", "darwin"):
    raise SystemExit("Builds currently support Linux and macOS")
subprocess.run(
    [
        os.environ.get("GO", "go"),
        "build",
        "-trimpath",
        "-buildmode=c-shared",
        "-o",
        str(root / "src/witness_client" / f"libwitnessproof.{extension}"),
        ".",
    ],
    cwd=root / "native",
    check=True,
)
subprocess.run(
    [sys.executable, "-m", "build", "--wheel", "--no-isolation"], cwd=root, check=True
)
