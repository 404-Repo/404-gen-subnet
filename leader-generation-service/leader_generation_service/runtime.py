"""Local command execution and source-integrity utilities."""

import hashlib
import os
import subprocess  # nosec B404
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def execute(
    argv: list[str],
    *,
    cwd: Path | None = None,
    data: bytes | None = None,
    timeout: int = 60,
    capture: bool = True,
) -> bytes:
    # Commands run without a shell or interactive Git prompts.
    result = subprocess.run(  # noqa: S603  # nosec B603
        argv,
        cwd=cwd,
        input=data,
        check=False,
        capture_output=capture,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout or b""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
