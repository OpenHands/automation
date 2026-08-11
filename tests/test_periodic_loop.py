"""Tests for the shared periodic background-loop helper."""

import asyncio
import logging

import pytest

from openhands.automation.utils.periodic_loop import run_periodic_loop


@pytest.fixture
def logger():
    return logging.getLogger("test.periodic_loop")


class TestRunPeriodicLoop:
    async def test_runs_until_shutdown_signal(self, logger):
        calls = []

        async def cycle():
            calls.append(1)

        shutdown_event = asyncio.Event()

        async def stop_after(n):
            while len(calls) < n:
                await asyncio.sleep(0.01)
            shutdown_event.set()

        await asyncio.gather(
            run_periodic_loop(
                cycle,
                interval_seconds=0.01,
                shutdown_event=shutdown_event,
                logger=logger,
                name="Test loop",
            ),
            stop_after(3),
        )

        assert len(calls) >= 3

    async def test_exits_immediately_when_already_shut_down(self, logger):
        calls = []

        async def cycle():
            calls.append(1)

        shutdown_event = asyncio.Event()
        shutdown_event.set()

        await run_periodic_loop(
            cycle,
            interval_seconds=60,
            shutdown_event=shutdown_event,
            logger=logger,
            name="Test loop",
        )

        assert calls == []

    async def test_failed_cycle_does_not_stop_the_loop(self, logger):
        calls = []

        async def cycle():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")

        shutdown_event = asyncio.Event()

        async def stop_after(n):
            while len(calls) < n:
                await asyncio.sleep(0.01)
            shutdown_event.set()

        await asyncio.gather(
            run_periodic_loop(
                cycle,
                interval_seconds=0.01,
                shutdown_event=shutdown_event,
                logger=logger,
                name="Test loop",
            ),
            stop_after(2),
        )

        assert len(calls) >= 2

    async def test_on_error_receives_the_exception(self, logger):
        errors = []

        async def cycle():
            raise ValueError("boom")

        shutdown_event = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.02)
            shutdown_event.set()

        await asyncio.gather(
            run_periodic_loop(
                cycle,
                interval_seconds=0.01,
                shutdown_event=shutdown_event,
                logger=logger,
                name="Test loop",
                on_error=errors.append,
            ),
            stop_soon(),
        )

        assert len(errors) >= 1
        assert all(isinstance(e, ValueError) for e in errors)

    async def test_runs_forever_without_shutdown_event_until_cancelled(self, logger):
        calls = []

        async def cycle():
            calls.append(1)

        task = asyncio.create_task(
            run_periodic_loop(
                cycle,
                interval_seconds=0.01,
                shutdown_event=None,
                logger=logger,
                name="Test loop",
            )
        )
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
