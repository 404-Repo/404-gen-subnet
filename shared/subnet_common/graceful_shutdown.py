import asyncio
import signal


class GracefulShutdown:
    def __init__(self) -> None:
        self._shutdown_event = asyncio.Event()

    @property
    def should_stop(self) -> bool:
        return self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    def setup_signal_handlers(self) -> None:
        """Set up SIGTERM/SIGINT handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_shutdown)

    def remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    async def wait(self, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        """Wait for a shutdown signal. Returns True if triggered, False if timed out."""
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
