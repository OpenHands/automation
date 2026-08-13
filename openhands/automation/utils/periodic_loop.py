"""Shared scaffolding for periodic background loops (poll on an interval,
exit promptly on shutdown, survive a failed cycle). Used by git_sync/loop.py;
scheduler.py/dispatcher.py/watchdog.py still hand-roll their own copy.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final


# Sleep used when a callable `interval_seconds` fails before it has ever
# resolved. Without it the first-pass seed would be 0.0 and the loop would
# spin at full speed for as long as the failure lasts.
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

    `interval_seconds` may be an async callable, re-resolved before every
    sleep, for loops whose cadence is runtime-configurable. It is resolved
    inside the same try/except as the cycle, so a failure to read it (e.g. a
    transient DB error) is logged and retried rather than killing the loop;
    the previous interval is reused for that sleep, or
    `_UNRESOLVED_INTERVAL_SECONDS` when it has never resolved.

    Exits promptly on `shutdown_event`. A cycle that raises is logged (via
    `on_error`, or `logger.exception` if omitted) without stopping the loop.
    """
    # `None` rather than 0.0 while a callable interval is still unresolved:
    # the callable is what opens a DB session in git_sync_loop, and a DB that
    # is briefly unavailable at startup made the very first resolution raise,
    # leaving a 0.0 sleep that pegged a core and logged a traceback per
    # iteration until the DB came back.
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
