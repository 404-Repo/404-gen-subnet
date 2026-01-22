import asyncio
import weakref


class GenerationStop:
    """Cancellation token for a generation task."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str = ""

    @property
    def is_canceled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def should_stop(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str) -> None:
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    async def wait(self, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        """Wait for cancellation. Returns True if canceled, False if timed out."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def wait_for_event(self, event: asyncio.Event) -> bool:
        """Wait for an event or cancellation. Returns True if event set, False if canceled."""
        if event.is_set():
            return True
        if self.is_canceled:
            return False

        cancel_task = asyncio.create_task(self._event.wait())
        event_task = asyncio.create_task(event.wait())

        done, pending = await asyncio.wait(
            [event_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        return event.is_set()


class GenerationStopManager:
    """Tracks generation stops and cancels them on shutdown."""

    def __init__(self) -> None:
        self._stops: weakref.WeakSet[GenerationStop] = weakref.WeakSet()

    def new_stop(self) -> GenerationStop:
        stop = GenerationStop()
        self._stops.add(stop)
        return stop

    def cancel_all(self, reason: str) -> None:
        for stop in list(self._stops):
            stop.cancel(reason)
