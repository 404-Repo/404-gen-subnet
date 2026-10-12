"""Check local authorization state before recovering a current-protocol result."""

import json
from pathlib import Path

from .errors import WitnessError
from .types import Session


def load_session(directory: Path) -> Session:
    path = directory / "session.json"
    if not path.exists():
        if (directory / "job.json").exists():
            raise WitnessError(
                "Worker startup did not complete; no generation can be recovered. "
                "Use release() with this ID before queueing a replacement",
                directory.name,
                code="queue_incomplete",
            )
        raise WitnessError(
            "Local recovery state is unavailable",
            directory.name,
            code="recovery_unavailable",
        )
    value: Session = json.loads(path.read_bytes())
    if value.get("protocol") != 2:
        raise WitnessError(
            "Only witness protocol 2 can be resumed",
            directory.name,
            code="unsupported_protocol",
        )
    if not (directory / "claim.json").is_file():
        raise WitnessError(
            "Exchange stopped before authorization; no generation can be recovered",
            directory.name,
            code="exchange_not_authorized",
        )
    return value
