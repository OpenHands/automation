"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline and marks them FAILED.  Protects against callback
failures (sandbox crash, network loss, OOM kill) that would otherwise leave
runs stuck forever.

The ``timeout_at`` column is set to ``started_at + max_duration`` when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``).
"""

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from automation.models import AutomationRun, AutomationRunStatus
from automation.utils.time import utcnow


logger = logging.getLogger("automation.watchdog")

# Default scan interval
WATCHDOG_INTERVAL_SECONDS = 120


async def mark_stale_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Find and mark stale RUNNING runs as FAILED.

    A run is stale if ``timeout_at < now()`` (pre-computed at dispatch time).
    Uses optimistic locking (``WHERE status = RUNNING``) so a concurrent
    completion callback wins without conflict.

    Returns the number of runs marked as stale.
    """
    now = utcnow()
    marked = 0

    async with session_factory() as session:
        result = await session.execute(
            select(AutomationRun.id, AutomationRun.timeout_at).where(
                AutomationRun.status == AutomationRunStatus.RUNNING,
                AutomationRun.timeout_at.isnot(None),
                AutomationRun.timeout_at < now,
            )
        )
        stale_rows = result.all()

        for run_id, timeout_at in stale_rows:
            # Optimistic locking: only update if still RUNNING
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.FAILED,
                    completed_at=now,
                    error_detail="Timed out: no completion callback received",
                )
            )
            db_result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
            if db_result.rowcount > 0:
                logger.warning(
                    "Run %s is stale (timeout_at=%s, now=%s), marked FAILED",
                    run_id,
                    timeout_at,
                    now,
                )
                marked += 1
            else:
                logger.info("Run %s already completed, skipping watchdog", run_id)

        if marked:
            await session.commit()

    return marked


async def watchdog_loop(
    session_factory: async_sessionmaker[AsyncSession],
    interval_seconds: int = WATCHDOG_INTERVAL_SECONDS,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main watchdog loop — scans for stale runs periodically."""
    logger.info(
        "Watchdog started, scanning every %ds",
        interval_seconds,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Watchdog received shutdown signal, exiting")
            break

        try:
            marked = await mark_stale_runs(session_factory)
            if marked:
                logger.info("Marked %d stale run(s) as FAILED", marked)
        except Exception:
            logger.exception("Error in watchdog scan")

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
                logger.info("Watchdog received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
