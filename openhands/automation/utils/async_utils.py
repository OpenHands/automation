"""Async helpers for running coroutines concurrently.

Adapted from
https://github.com/OpenHands/OpenHands/blob/main/openhands/app_server/utils/async_utils.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from typing import Any


GENERAL_TIMEOUT: int = 15


class AsyncException(Exception):
    """Raised by ``wait_all`` when more than one task raised an exception."""

    def __init__(self, exceptions: list[BaseException]) -> None:
        self.exceptions = exceptions
        super().__init__("\n".join(str(e) for e in exceptions))


async def wait_all(
    iterable: Iterable[Coroutine[Any, Any, Any]],
    timeout: float | None = GENERAL_TIMEOUT,
) -> list[Any]:
    """Run the given coroutines concurrently and wait for them all to finish.

    Returns the results in the original order. If a single task raised an
    exception it is re-raised. If multiple tasks raised, an
    :class:`AsyncException` containing all of them is raised. If the timeout
    elapses any still-pending tasks are cancelled and ``asyncio.TimeoutError``
    is raised.
    """
    tasks = [asyncio.create_task(c) for c in iterable]
    if not tasks:
        return []

    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        for task in pending:
            task.cancel()
        raise TimeoutError()

    results: list[Any] = []
    errors: list[BaseException] = []
    for task in tasks:
        try:
            results.append(task.result())
        except Exception as e:  # noqa: BLE001 - propagated below
            errors.append(e)
            results.append(None)

    if errors:
        if len(errors) == 1:
            raise errors[0]
        raise AsyncException(errors)
    return results
