import asyncio
from datetime import UTC, datetime

import pytest
from loguru import logger
from subnet_common.graceful_shutdown import GracefulShutdown

import main as service_main
from validation_service.settings import Settings


async def test_cycle_error_is_logged_and_retried_with_graceful_shutdown(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    shutdown = GracefulShutdown()
    monkeypatch.setattr(service_main, "Settings", lambda: settings)
    monkeypatch.setattr(service_main, "GracefulShutdown", lambda: shutdown)
    monkeypatch.setattr(service_main, "setup_logging", lambda log_level: None)
    handlers: list[str] = []
    monkeypatch.setattr(shutdown, "setup_signal_handlers", lambda: handlers.append("setup"))
    monkeypatch.setattr(shutdown, "remove_signal_handlers", lambda: handlers.append("remove"))
    calls = 0
    waits: list[float | None] = []

    async def run_iteration(settings: Settings, shutdown: GracefulShutdown) -> datetime | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("GitHub unavailable")
        shutdown.request_shutdown()
        return datetime(2000, 1, 1, tzinfo=UTC)

    async def wait(**kwargs: float | None) -> bool:
        waits.append(kwargs["timeout"])
        return shutdown.should_stop

    monkeypatch.setattr(service_main, "run_validation_iteration", run_iteration)
    monkeypatch.setattr(shutdown, "wait", wait)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        await service_main.main()
    finally:
        logger.remove(sink)

    assert calls == 2
    assert waits == [settings.max_check_state_interval_seconds, settings.min_check_state_interval_seconds]
    assert handlers == ["setup", "remove"]
    assert "Validation cycle failed: GitHub unavailable" in messages
    assert messages[-1] == "Validation service stopped gracefully"


async def test_cancelled_iteration_stops_the_service(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    shutdown = GracefulShutdown()
    monkeypatch.setattr(service_main, "Settings", lambda: settings)
    monkeypatch.setattr(service_main, "GracefulShutdown", lambda: shutdown)
    monkeypatch.setattr(service_main, "setup_logging", lambda log_level: None)
    monkeypatch.setattr(shutdown, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(shutdown, "remove_signal_handlers", lambda: None)

    async def run_iteration(settings: Settings, shutdown: GracefulShutdown) -> datetime | None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service_main, "run_validation_iteration", run_iteration)
    await service_main.main()
