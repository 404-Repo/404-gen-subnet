"""VRAM detection utilities."""

import subprocess

from loguru import logger


# H200 SXM: 141 GB HBM3e per GPU
EXPECTED_VRAM_PER_GPU_GB = 141
EXPECTED_GPU_COUNT = 4
EXPECTED_TOTAL_VRAM_GB = EXPECTED_VRAM_PER_GPU_GB * EXPECTED_GPU_COUNT  # 564 GB


def get_total_vram_gb() -> float | None:
    """Query total VRAM across all GPUs via nvidia-smi. Returns GB or None if unavailable."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"nvidia-smi failed: {result.stderr.strip()}")
            return None

        total_mb = sum(float(line.strip()) for line in result.stdout.strip().splitlines() if line.strip())
        return total_mb / 1024

    except FileNotFoundError:
        logger.warning("nvidia-smi not found — no GPU available")
        return None
    except Exception as e:
        logger.warning(f"VRAM detection failed: {e}")
        return None


def check_vram_adequate() -> tuple[bool, float | None]:
    """Check if total VRAM meets the expected 4xH200 threshold.

    Returns (is_adequate, total_vram_gb).
    """
    total = get_total_vram_gb()
    if total is None:
        return False, None
    # Allow 5% tolerance for firmware/driver overhead
    threshold = EXPECTED_TOTAL_VRAM_GB * 0.95
    return total >= threshold, total
