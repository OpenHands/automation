"""Periodic background loop: poll on an interval, exit on shutdown, survive a
failed cycle. Used by git_sync/loop.py; scheduler/dispatcher/watchdog still
hand-roll their own.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final


# Sleep when a callable `interval_seconds` fails before ever resolving; a 0.0
# seed would spin the loop at full speed until the failure clears.
_UNRESOLVED_INTERVAL_SECONDS: Final[float] = 5.0


async def run_periodic_loop(
    run_cycle: Callable[[], Awaitable[None]],
    *,
    interval_seconds: float | Callable[[], Awaitable[float]],
    shutdown_event: asyncio.Event | None,
    logger: logging.Logger,
    name: str,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run `run_cycle()` repeatedly, sleeping `interval_seconds` in between.

    `interval_seconds` may be an async callable, re-resolved before every sleep
    for runtime-configurable cadences. It resolves inside the cycle's
    try/except, so a failed read reuses the previous interval (or
    `_UNRESOLVED_INTERVAL_SECONDS`) instead of killing the loop.

    Exits promptly on `shutdown_event`. A cycle that raises is logged (via
    `on_error`, else `logger.exception`) without stopping the loop.
    """
    # `None`, not 0.0, while a callable interval is unresolved: if the first
    # resolution raises (e.g. DB down at startup), 0.0 would busy-spin.
    interval = None if callable(interval_seconds) else interval_seconds

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("%s received shutdown signal, exiting", name)
            break

        try:
            if callable(interval_seconds):
                interval = await interval_seconds()
            await run_cycle()
        except Exception as e:
            if on_error is not None:
                on_error(e)
            else:
                logger.exception("Error in %s cycle", name)

        sleep_for = _UNRESOLVED_INTERVAL_SECONDS if interval is None else interval
        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_for)
                logger.info("%s received shutdown signal, exiting", name)
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(sleep_for)
