"""Tests for openhands.automation.utils.async_utils."""

import asyncio

import pytest

from openhands.automation.utils.async_utils import AsyncException, wait_all


class TestWaitAll:
    async def test_empty_iterable_returns_empty_list(self):
        assert await wait_all([]) == []

    async def test_results_in_original_order(self):
        async def producer(value: int, delay: float) -> int:
            await asyncio.sleep(delay)
            return value

        # The slowest coroutine is first; result order must still match input.
        results = await wait_all(
            [producer(1, 0.03), producer(2, 0.0), producer(3, 0.01)]
        )
        assert results == [1, 2, 3]

    async def test_runs_concurrently(self):
        # Three 50ms sleeps must complete well under 150ms (serial baseline).
        async def sleeper() -> int:
            await asyncio.sleep(0.05)
            return 1

        loop = asyncio.get_running_loop()
        start = loop.time()
        results = await wait_all([sleeper(), sleeper(), sleeper()])
        elapsed = loop.time() - start
        assert results == [1, 1, 1]
        assert elapsed < 0.13

    async def test_single_exception_propagates(self):
        async def boom() -> None:
            raise RuntimeError("kapow")

        async def ok() -> int:
            return 42

        with pytest.raises(RuntimeError, match="kapow"):
            await wait_all([ok(), boom()])

    async def test_multiple_exceptions_wrapped(self):
        async def one() -> None:
            raise ValueError("a")

        async def two() -> None:
            raise ValueError("b")

        with pytest.raises(AsyncException) as exc:
            await wait_all([one(), two()])
        assert len(exc.value.exceptions) == 2

    async def test_timeout_cancels_pending(self):
        async def slow() -> None:
            await asyncio.sleep(1)

        with pytest.raises(asyncio.TimeoutError):
            await wait_all([slow()], timeout=0.05)
