"""GPU health benchmark for detecting degraded hardware.

Runs a matrix-multiply stress test on each GPU independently (in parallel via
spawned subprocesses) and checks per-GPU VRAM. Returns per-GPU throughput in
TFLOPS and memory in GB so the caller can decide whether to request a pod
replacement.

A GPU "passes" if BOTH its compute throughput meets the TFLOPS threshold AND
its total VRAM meets the per-GPU VRAM threshold. Either failure individually
is enough to mark the GPU as degraded.

Configuration (environment variables):
    BENCHMARK_GPU_COUNT      Number of GPUs to test          (default: 4)
    BENCHMARK_MATRIX_SIZE    Square matrix dimension          (default: 4096)
    BENCHMARK_DURATION_SEC   Seconds to run per GPU           (default: 3.0)
    BENCHMARK_MIN_TFLOPS     Minimum FP32 TFLOPS to pass     (default: 30.0)
    BENCHMARK_MIN_VRAM_GB    Minimum per-GPU VRAM in GB      (default: 134.0)
                             H200 SXM has 141 GB; 134 = 141 × 0.95 tolerance

Torch is required. The caller (service.py) catches ImportError and skips the
benchmark gracefully when torch is not installed.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass


GPU_COUNT = int(os.environ.get("BENCHMARK_GPU_COUNT", "4"))
MATRIX_SIZE = int(os.environ.get("BENCHMARK_MATRIX_SIZE", "4096"))
DURATION_SEC = float(os.environ.get("BENCHMARK_DURATION_SEC", "3.0"))
MIN_TFLOPS = float(os.environ.get("BENCHMARK_MIN_TFLOPS", "30.0"))
MIN_VRAM_GB = float(os.environ.get("BENCHMARK_MIN_VRAM_GB", "134.0"))


@dataclass
class GPUBenchmarkResult:
    gpu_id: int
    gpu_name: str
    # Compute benchmark
    ops: int
    elapsed_sec: float
    tflops: float
    compute_passed: bool
    # VRAM check
    vram_gb: float
    vram_passed: bool
    # Overall — True only if both compute AND VRAM pass.
    passed: bool


def _benchmark_single_gpu(
    gpu_id: int,
    matrix_size: int,
    duration_sec: float,
    min_tflops: float,
    min_vram_gb: float,
) -> GPUBenchmarkResult:
    """Stress-test one GPU with repeated matrix multiplications and check VRAM.

    Meant to run in a spawned subprocess — each process initialises its own
    CUDA context on the target device.
    """
    import torch

    torch.cuda.set_device(gpu_id)
    device = torch.device(f"cuda:{gpu_id}")
    gpu_name = torch.cuda.get_device_name(gpu_id)

    # VRAM check — read total memory from the device properties.
    props = torch.cuda.get_device_properties(gpu_id)
    vram_gb = round(props.total_mem / (1024**3), 1)
    vram_passed = vram_gb >= min_vram_gb

    # Compute benchmark — sustained matrix multiply throughput.
    a = torch.randn(matrix_size, matrix_size, device=device)
    b = torch.randn(matrix_size, matrix_size, device=device)

    # Warmup — let the driver finish any lazy init before timing.
    for _ in range(3):
        torch.mm(a, b)
    torch.cuda.synchronize(device)

    ops = 0
    start = time.monotonic()
    while time.monotonic() - start < duration_sec:
        torch.mm(a, b)
        torch.cuda.synchronize(device)
        ops += 1
    elapsed = time.monotonic() - start

    flops_per_op = 2.0 * matrix_size**3
    tflops = round((ops * flops_per_op) / elapsed / 1e12, 2)
    compute_passed = tflops >= min_tflops

    return GPUBenchmarkResult(
        gpu_id=gpu_id,
        gpu_name=gpu_name,
        ops=ops,
        elapsed_sec=round(elapsed, 2),
        tflops=tflops,
        compute_passed=compute_passed,
        vram_gb=vram_gb,
        vram_passed=vram_passed,
        passed=compute_passed and vram_passed,
    )


def run_benchmark(
    num_gpus: int = GPU_COUNT,
    matrix_size: int = MATRIX_SIZE,
    duration_sec: float = DURATION_SEC,
    min_tflops: float = MIN_TFLOPS,
    min_vram_gb: float = MIN_VRAM_GB,
) -> list[GPUBenchmarkResult]:
    """Benchmark *num_gpus* GPUs in parallel and return per-GPU results."""
    import torch.multiprocessing as mp

    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=num_gpus, mp_context=ctx) as pool:
        futures = [
            pool.submit(
                _benchmark_single_gpu,
                gpu_id,
                matrix_size,
                duration_sec,
                min_tflops,
                min_vram_gb,
            )
            for gpu_id in range(num_gpus)
        ]
        return [f.result() for f in futures]
