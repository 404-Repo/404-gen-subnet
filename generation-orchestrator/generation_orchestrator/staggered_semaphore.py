import asyncio
from types import TracebackType
from typing import Self


class StaggeredSemaphore:
    """Semaphore with delay between acquisitions."""

    def __init__(self, value: int, delay: float = 5.0):
        self._semaphore = asyncio.Semaphore(value)
        self._delay = delay
        self._lock = asyncio.Lock()
        self._last_acquire = 0.0

    async def __aenter__(self) -> Self:
        await self._semaphore.acquire()
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._last_acquire + self._delay - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_acquire = asyncio.get_running_loop().time()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._semaphore.release()
