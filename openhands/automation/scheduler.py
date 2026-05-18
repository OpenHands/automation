"""Background scheduler for polling due cron automations.

Runs as an in-process background task within the FastAPI app. Polls the database
every N seconds (configurable via AUTOMATION_SCHEDULER_INTERVAL_SECONDS) for
enabled cron automations whose next fire time is due.

Uses FOR UPDATE SKIP LOCKED for multi-worker safety in PostgreSQL.
SQLite deployments skip row locking (single-process mode assumed).
"""

import asyncio
import logging
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.db import using_sqlite
from openhands.automation.models import Automation, AutomationRun
from openhands.automation.schemas import TriggerAdapter
from openhands.automation.utils import utcnow


logger = logging.getLogger("automation.scheduler")

# Default batch size for polling
DEFAULT_BATCH_SIZE = 50

# Minimum interval between polling the same automation (seconds)
POLL_INTERVAL_SECONDS = 60


async def _fetch_enabled_automations(
    session: AsyncSession,
    batch_size: int,
    poll_threshold: datetime,
) -> list[Automation]:
    """Fetch enabled automations, optionally using FOR UPDATE SKIP LOCKED.

    For PostgreSQL: Uses FOR UPDATE SKIP LOCKED so multiple workers can poll
    concurrently without picking the same rows. Each worker claims a batch atomically.

    For SQLite: Skips row locking (not supported). SQLite deployments assume
    single-process mode where row locking isn't needed.

    The poll_threshold filters out automations that were recently polled,
    ensuring fair rotation through all automations when using batching.

    Args:
        session: Database session
        batch_size: Maximum number of automations to fetch
        poll_threshold: Only poll automations not polled since this time

    Returns:
        List of claimed automations
    """
    select_query = (
        select(Automation)
        .where(
            Automation.enabled.is_(True),
            Automation.deleted_at.is_(None),
            (Automation.last_polled_at.is_(None))
            | (Automation.last_polled_at < poll_threshold),
        )
        .order_by(Automation.last_polled_at.asc().nulls_first())
        .limit(batch_size)
    )

    # Apply row locking for PostgreSQL only (SQLite doesn't support it)
    if not using_sqlite():
        select_query = select_query.with_for_update(skip_locked=True)

    result = await session.execute(select_query)
    return list(result.scalars().all())


async def _create_pending_runs(
    session: AsyncSession,
    automations: list[Automation],
    now: datetime,
) -> list[AutomationRun]:
    """Ask each automation's trigger to create a PENDING run if it's due.

    Each automation's ``trigger`` JSON is parsed into a typed model
    (``CronTrigger``/``EventTrigger``/``GithubTrigger``) and its
    :meth:`_TriggerBase.create_pending_run` is awaited. A return value of
    ``None`` means "not due"; any other return is appended to the result.

    Calls run **sequentially** because they share a single
    :class:`AsyncSession` (SQLAlchemy async sessions are not safe for
    concurrent use). Triggers that need to fan out external I/O — e.g.
    :class:`~openhands.automation.schemas.GithubTrigger` polling multiple
    repos — do so internally.

    Per-trigger exceptions are logged and skipped so one bad trigger cannot
    starve the rest of the batch.
    """
    created: list[AutomationRun] = []
    for automation in automations:
        try:
            trigger = TriggerAdapter.validate_python(automation.trigger)
        except ValidationError:
            logger.exception("Invalid trigger config for automation %s", automation.id)
            continue

        try:
            run = await trigger.create_pending_run(session, automation, now)
        except Exception:
            logger.exception(
                "Trigger %s raised while processing automation %s",
                type(trigger).__name__,
                automation.id,
            )
            continue

        if run is None:
            continue

        created.append(run)
        logger.info(
            "Created pending run: run_id=%s automation_id=%s name=%s trigger_type=%s",
            run.id,
            automation.id,
            automation.name,
            automation.trigger.get("type"),
        )

    return created


async def poll_and_schedule(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[AutomationRun]:
    """Poll for due automations and create pending runs atomically.

    Fetches enabled automations (using FOR UPDATE SKIP LOCKED on PostgreSQL for
    multi-worker safety), updates last_polled_at for ALL fetched automations
    (to ensure fair batch rotation), filters to those that are due, and creates
    PENDING runs. All within a single transaction so row locks are held throughout
    and no schedules can be lost or duplicated.

    Note: SQLite deployments skip row locking (single-process mode assumed).

    Args:
        session_factory: SQLAlchemy async session factory
        batch_size: Maximum number of automations to poll per batch

    Returns:
        List of AutomationRun objects created
    """
    now = utcnow()
    poll_threshold = now - timedelta(seconds=POLL_INTERVAL_SECONDS)
    created_runs: list[AutomationRun] = []

    async with session_factory() as session:
        automations = await _fetch_enabled_automations(
            session, batch_size, poll_threshold
        )

        # Update last_polled_at for ALL fetched automations to ensure fair
        # batch rotation. Without this, non-due automations would be re-polled
        # every cycle, starving other automations in subsequent batches.
        if automations:
            automation_ids = [a.id for a in automations]
            await session.execute(
                update(Automation)
                .where(Automation.id.in_(automation_ids))
                .values(last_polled_at=now)
            )
            for automation in automations:
                automation.last_polled_at = now

        created_runs.extend(await _create_pending_runs(session, automations, now))

        # Always commit to release row locks from FOR UPDATE SKIP LOCKED,
        # even if no runs were created
        await session.commit()

    return created_runs


async def scheduler_loop(
    session_factory: async_sessionmaker[AsyncSession],
    interval_seconds: int = 60,
    shutdown_event: asyncio.Event | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Main scheduler loop that polls for due automations.

    For each due automation, creates a PENDING run in the automation_runs table.
    The dispatcher (separate process) picks up PENDING runs and executes them.

    Args:
        session_factory: SQLAlchemy async session factory
        interval_seconds: Polling interval in seconds
        shutdown_event: Event to signal shutdown (for graceful stop)
        batch_size: Maximum number of automations to poll per batch
    """
    logger.info(
        "Scheduler started, polling every %d seconds (batch_size=%d)",
        interval_seconds,
        batch_size,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Scheduler received shutdown signal, exiting")
            break

        try:
            created_runs = await poll_and_schedule(
                session_factory, batch_size=batch_size
            )

            if created_runs:
                logger.info(
                    "Found %d due automation(s) to schedule",
                    len(created_runs),
                )
            else:
                logger.debug("No automations due at this time")

        except Exception:
            logger.exception("Error in scheduler poll cycle")

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=interval_seconds,
                )
                logger.info("Scheduler received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
