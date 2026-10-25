"""Runner-owned final saving with a private checkpoint before network delivery."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from renderer_client import ArtifactClient, RenderOptions, RenderRequest
from witness_client.calls import canonical
from witness_client.state import save


def run_and_save(
    generate: Callable[[], str | None],
    client: ArtifactClient,
    checkpoint: Path,
) -> dict[str, object]:
    """Retry final-save delivery without calling generate again after its return."""
    checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if checkpoint.parent.stat().st_mode & 0o077:
        raise ValueError("Final-save directory must be private (chmod 700)")
    with checkpoint.with_suffix(checkpoint.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = {
            "generation": client.generation,
            "renderer_id": client.renderer_id,
            "submission_commit": client.submission_commit,
        }
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_bytes())
            if saved["identity"] != identity:
                raise ValueError("Final-save checkpoint context differs")
            output = saved["output"]
        else:
            output = generate()
            if output is not None and not isinstance(output, str):
                raise ValueError("generate() must return JavaScript or None")
            if output is not None:
                RenderRequest.from_source(client.renderer_id, output, RenderOptions())
            saved = {
                "identity": identity,
                "output": output,
                "sha256": None if output is None else hashlib.sha256(output.encode()).hexdigest(),
            }
            save(checkpoint, canonical(saved))
        if output is not None and not isinstance(output, str):
            raise ValueError("Invalid checkpoint output")
        digest = None if output is None else hashlib.sha256(output.encode()).hexdigest()
        if saved["sha256"] != digest:
            raise ValueError("Final-save checkpoint bytes changed")
        return client.save_final(output)
