"""Placeholder generator for the reference miner service.

Returns the canonical reference module (``examples/car.js``) for every prompt.
Replace ``generate_car_scene`` with your own generation logic — the rest of
``service.py`` (ZIP packing, failure manifest, state machine) does not change.

The returned bytes are UTF-8 JavaScript source conforming to:
    output_specifications.md § Function Signature / Execution Constraints
    runtime_specifications.md § Static Analysis / Post-Execution Validation

Validate with: ``node tools/validate.js examples/car.js``
"""

from functools import lru_cache
from pathlib import Path


# Canonical reference module shipped with miner-reference.
# Resolved relative to this file so it works regardless of the caller's cwd.
_REFERENCE_CAR_JS = Path(__file__).resolve().parent.parent / "examples" / "car.js"


@lru_cache(maxsize=1)
def _load_reference_source() -> bytes:
    """Read the reference car.js once and cache the bytes.

    Raises FileNotFoundError at first call if the expected layout is missing —
    surfacing the configuration problem immediately instead of silently
    returning an empty or malformed response.
    """
    if not _REFERENCE_CAR_JS.is_file():
        raise FileNotFoundError(
            f"Reference module not found at {_REFERENCE_CAR_JS}. "
            "The placeholder service expects the miner-reference project layout "
            "with examples/car.js sitting next to miner_reference/."
        )
    return _REFERENCE_CAR_JS.read_bytes()


def generate_car_scene() -> bytes:
    """Return a spec-compliant generate.js module as UTF-8 bytes.

    Returns the same car module for every prompt (placeholder behavior).
    """
    return _load_reference_source()
