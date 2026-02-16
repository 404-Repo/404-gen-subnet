"""Tests for generation_orchestrator.generation_stop module."""

import asyncio

import pytest

from generation_orchestrator.generation_stop import GenerationStop, GenerationStopManager


class TestGenerationStop:
    """Unit tests for the GenerationStop cancellation token."""

    def test_initial_state_not_canceled(self) -> None:
        stop = GenerationStop()
        assert stop.is_canceled is False
        assert stop.should_stop is False
        assert stop.reason == ""

    def test_cancel_sets_state(self) -> None:
        stop = GenerationStop()
        stop.cancel("timeout")
        assert stop.is_canceled is True
        assert stop.should_stop is True
        assert stop.reason == "timeout"

    def test_cancel_idempotent(self) -> None:
        """Second cancel should not overwrite the reason."""
        stop = GenerationStop()
        stop.cancel("first reason")
        stop.cancel("second reason")
        assert stop.reason == "first reason"

    @pytest.mark.asyncio
    async def test_wait_returns_true_on_cancel(self) -> None:
        stop = GenerationStop()
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, stop.cancel, "delayed cancel")
        result = await stop.wait(timeout=2.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_returns_false_on_timeout(self) -> None:
        stop = GenerationStop()
        result = await stop.wait(timeout=0.05)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_event_returns_true_when_event_set(self) -> None:
        stop = GenerationStop()
        event = asyncio.Event()
        event.set()
        result = await stop.wait_for_event(event)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_event_returns_false_when_canceled(self) -> None:
        stop = GenerationStop()
        event = asyncio.Event()
        stop.cancel("shutdown")
        result = await stop.wait_for_event(event)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_event_with_delayed_event(self) -> None:
        stop = GenerationStop()
        event = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, event.set)
        result = await stop.wait_for_event(event)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_event_cancel_wins_over_unset_event(self) -> None:
        stop = GenerationStop()
        event = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, stop.cancel, "cancel wins")
        result = await stop.wait_for_event(event)
        assert result is False


class TestGenerationStopManager:
    """Unit tests for the GenerationStopManager."""

    def test_new_stop_returns_fresh_instance(self) -> None:
        mgr = GenerationStopManager()
        s1 = mgr.new_stop()
        s2 = mgr.new_stop()
        assert s1 is not s2
        assert not s1.is_canceled
        assert not s2.is_canceled

    def test_cancel_all(self) -> None:
        mgr = GenerationStopManager()
        stops = [mgr.new_stop() for _ in range(5)]
        mgr.cancel_all("global shutdown")
        for s in stops:
            assert s.is_canceled
            assert s.reason == "global shutdown"

    def test_weak_references_allow_gc(self) -> None:
        """Stops that go out of scope should be garbage collected."""
        mgr = GenerationStopManager()
        mgr.new_stop()  # not stored
        # After GC, cancel_all should not fail
        import gc
        gc.collect()
        mgr.cancel_all("cleanup")  # should not raise

    def test_cancel_all_on_empty_manager(self) -> None:
        mgr = GenerationStopManager()
        mgr.cancel_all("nothing to cancel")  # should not raise
