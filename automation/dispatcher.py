"""Dispatcher for processing pending automation runs.

Polls the automation_runs table for PENDING jobs and dispatches them
for execution. Uses the same database-specific strategies as the scheduler
for multi-worker safety.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from automation.models import AutomationRun, AutomationRunStatus
from automation.utils.run import mark_run_status


logger = logging.getLogger("automation.dispatcher")

# Default batch size for polling pending runs
DEFAULT_BATCH_SIZE = 10

# Minimum interval between polling the same run (seconds)
POLL_INTERVAL_SECONDS = 30


async def _poll_pending_runs(
    session: AsyncSession,
    batch_size: int,
) -> list[AutomationRun]:
    """Poll pending runs using FOR UPDATE SKIP LOCKED.

    This allows multiple workers to dispatch concurrently without
    claiming the same runs.

    Args:
        session: Database session
        batch_size: Maximum number of runs to fetch

    Returns:
        List of claimed pending runs
    """
    select_query = (
        select(AutomationRun)
        .where(AutomationRun.status == AutomationRunStatus.PENDING)
        .order_by(AutomationRun.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )

    result = await session.execute(select_query)
    return list(result.scalars().all())


async def dispatch_pending_runs(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[AutomationRun]:
    """Poll for pending runs and mark them as started.

    Uses FOR UPDATE SKIP LOCKED for multi-worker safety, allowing
    multiple dispatchers to run concurrently without claiming the
    same runs.

    Args:
        session_factory: SQLAlchemy async session factory
        batch_size: Maximum number of runs to process per batch

    Returns:
        List of runs that were dispatched (marked as RUNNING)
    """
    async with session_factory() as session:
        pending_runs = await _poll_pending_runs(session, batch_size)

        dispatched_runs = []

        for run in pending_runs:
            try:
                # PHASE 1 LIMITATION: Runs are marked RUNNING but not executed.
                # Phase 1b will integrate with OpenHands SaaS API to actually
                # execute the user's tarball/entrypoint. For now, this is a
                # CRUD API + scheduler queue only.
                logger.info("Dispatching automation run %s", run.id)
                await mark_run_status(session, run, AutomationRunStatus.RUNNING)
                dispatched_runs.append(run)
                # TODO(Phase 1b): Call SaaS API to create conversation
            except Exception:
                logger.exception("Failed to dispatch run %s", run.id)

        # Always commit to release row locks from FOR UPDATE SKIP LOCKED,
        # even if no runs were dispatched
        await session.commit()

        return dispatched_runs


async def dispatcher_loop(
    session_factory: async_sessionmaker[AsyncSession],
    interval_seconds: int = POLL_INTERVAL_SECONDS,
    shutdown_event: asyncio.Event | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Main dispatcher loop that polls for pending runs and dispatches them.

    Args:
        session_factory: SQLAlchemy async session factory
        interval_seconds: Polling interval in seconds
        shutdown_event: Event to signal shutdown (for graceful stop)
        batch_size: Maximum number of runs to process per batch
    """
    logger.info(
        "Dispatcher started, polling every %d seconds (batch_size=%d)",
        interval_seconds,
        batch_size,
    )

    while True:
        # Check for shutdown signal
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Dispatcher received shutdown signal, exiting")
            break

        try:
            dispatched = await dispatch_pending_runs(
                session_factory, batch_size=batch_size
            )

            if dispatched:
                logger.info("Dispatched %d run(s)", len(dispatched))
            else:
                logger.debug("No pending runs to dispatch")

        except Exception:
            logger.error("Error dispatching pending runs", exc_info=True)

        # Wait for the next poll interval (or shutdown)
        if shutdown_event is not None:
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=interval_seconds,
                )
                # If we get here, shutdown was signaled
                logger.info("Dispatcher received shutdown signal, exiting")
                break
            except TimeoutError:
                # Normal timeout, continue polling
                pass
        else:
            await asyncio.sleep(interval_seconds)
