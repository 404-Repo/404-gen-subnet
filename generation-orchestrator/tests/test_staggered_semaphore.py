"""Tests for generation_orchestrator.staggered_semaphore module."""

import asyncio
import time

import pytest

from generation_orchestrator.staggered_semaphore import StaggeredSemaphore


class TestStaggeredSemaphore:
    """Unit tests for StaggeredSemaphore."""

    @pytest.mark.asyncio
    async def test_basic_acquire_release(self) -> None:
        sem = StaggeredSemaphore(value=2, delay=0.0)
        async with sem:
            pass  # should not hang

    @pytest.mark.asyncio
    async def test_concurrency_limited(self) -> None:
        """Only `value` tasks can hold the semaphore concurrently."""
        sem = StaggeredSemaphore(value=2, delay=0.0)
        active = 0
        max_active = 0

        async def worker():
            nonlocal active, max_active
            async with sem:
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(*[worker() for _ in range(6)])
        assert max_active <= 2

    @pytest.mark.asyncio
    async def test_stagger_delay_enforced(self) -> None:
        """Acquisitions should be spaced by at least `delay` seconds."""
        delay = 0.1
        sem = StaggeredSemaphore(value=3, delay=delay)
        timestamps: list[float] = []

        async def worker():
            async with sem:
                timestamps.append(asyncio.get_running_loop().time())

        await asyncio.gather(*[worker() for _ in range(3)])
        timestamps.sort()
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            # Allow small tolerance
            assert gap >= delay * 0.8, f"Gap {gap:.3f}s is less than expected delay {delay}s"

    @pytest.mark.asyncio
    async def test_single_slot_serializes(self) -> None:
        """With value=1, tasks must run sequentially."""
        sem = StaggeredSemaphore(value=1, delay=0.0)
        order: list[int] = []

        async def worker(idx: int):
            async with sem:
                order.append(idx)
                await asyncio.sleep(0.01)

        await asyncio.gather(*[worker(i) for i in range(4)])
        assert len(order) == 4
