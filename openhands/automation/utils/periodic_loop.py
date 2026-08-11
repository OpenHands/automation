"""Shared scaffolding for periodic background loops (poll on an interval,
exit promptly on shutdown, survive a failed cycle). Used by git_sync/loop.py;
scheduler.py/dispatcher.py/watchdog.py still hand-roll their own copy.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable


async def run_periodic_loop(
    run_cycle: Callable[[], Awaitable[None]],
    *,
    interval_seconds: float,
    shutdown_event: asyncio.Event | None,
    logger: logging.Logger,
    name: str,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run `run_cycle()` repeatedly, sleeping `interval_seconds` in between.

    Exits promptly on `shutdown_event`. A cycle that raises is logged (via
    `on_error`, or `logger.exception` if omitted) without stopping the loop.
    """
    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("%s received shutdown signal, exiting", name)
            break

        try:
            await run_cycle()
        except Exception as e:
            if on_error is not None:
                on_error(e)
            else:
                logger.exception("Error in %s cycle", name)

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
                logger.info("%s received shutdown signal, exiting", name)
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
