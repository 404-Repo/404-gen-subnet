"""Resolve a miner's declared hardware tokens to a deployable GPU spec.

A miner declares one or more verification configurations in `hardware.json` (recorded on
`MinerSubmission.hardware`). Declaring several means "my pipeline runs on any of them" — the
orchestrator picks one per its own preference order and verifies the miner there in a single
run. This module owns that token -> (gpu_type, gpu_count) mapping and the selection.
"""

from loguru import logger
from subnet_common.competition.submissions import DEFAULT_HARDWARE


# Token -> (gpu_type, gpu_count). The token name is the spec: "4xH200" is 4× H200. `gpu_type`
# is the canonical model string the GPU provider clients map to their own SKUs.
HARDWARE_SPECS: dict[str, tuple[str, int]] = {
    "4xH200": ("H200", 4),
    "4xRTX6000Pro": ("RTX6000Pro", 4),
}

# Orchestrator preference order, most-preferred first. RTX 6000 Pro leads because it is the
# cheaper card: a miner that supports it is verified there. H200 is the fallback.
HARDWARE_PREFERENCE: tuple[str, ...] = ("4xRTX6000Pro", "4xH200")


def select_hardware(
    declared: list[str], log_id: str, preference: tuple[str, ...] = HARDWARE_PREFERENCE
) -> tuple[str, int]:
    """Choose the (gpu_type, gpu_count) to verify a miner on from the configs they declared.

    Picks the first config in the orchestrator's `preference` order that the miner declared.
    Falls back to the miner's own first deployable config, then to the default, so the return
    is always deployable. The submission-collector already validates tokens against the
    supported set, so the fallbacks only fire on misconfiguration.
    """
    for token in preference:
        if token in declared and token in HARDWARE_SPECS:
            logger.info(f"{log_id}: verifying on {token} (declared {declared})")
            return HARDWARE_SPECS[token]

    for token in declared:
        if token in HARDWARE_SPECS:
            logger.warning(f"{log_id}: declared {declared} matches no preferred config {preference}; using {token}")
            return HARDWARE_SPECS[token]

    logger.warning(f"{log_id}: no deployable hardware in {declared}; defaulting to {DEFAULT_HARDWARE}")
    return HARDWARE_SPECS[DEFAULT_HARDWARE]
