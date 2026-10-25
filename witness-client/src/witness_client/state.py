"""Durable, private recovery files."""

import json
import os
import tempfile
import time
from pathlib import Path

from .errors import WitnessError
from .types import JSONObject


def save(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remaining_seconds(state: Path, identifier: str) -> float:
    """Return seconds left before the exchange's job deadline.

    Read the gateway deadline from ``state/<identifier>/job.json``. It covers both
    preparation and execution. Raise WitnessError if the deadline has passed.
    """
    job: JSONObject = json.loads(
        (state / identifier / "job.json").read_bytes()
    )
    remaining = float(str(job.get("expires", job["deadline"]))) - time.time()
    if remaining <= 0:
        raise WitnessError(
            "Queued exchange deadline reached", identifier, source="gateway"
        )
    return remaining
