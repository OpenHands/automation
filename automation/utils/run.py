"""Automation run utilities."""

import uuid
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from automation.models import Automation, AutomationRun, AutomationRunStatus
from automation.utils.time import utcnow


# Default maximum run duration before the watchdog marks it as stale
DEFAULT_MAX_RUN_DURATION = timedelta(hours=1)


async def create_pending_run(
    session: AsyncSession,
    automation: Automation,
) -> AutomationRun:
    """Create a PENDING automation run for dispatch.

    Also updates the automation's last_triggered_at and last_polled_at
    timestamps. Caller is responsible for committing the transaction.

    Args:
        session: Database session
        automation: The automation to create a run for

    Returns:
        The created AutomationRun
    """
    now = utcnow()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.PENDING,
    )
    session.add(run)

    await session.execute(
        update(Automation)
        .where(Automation.id == automation.id)
        .values(last_triggered_at=now, last_polled_at=now)
    )

    # Update the in-memory object for consistency with the database
    automation.last_triggered_at = now
    automation.last_polled_at = now

    return run


async def mark_run_status(
    session: AsyncSession,
    run: AutomationRun,
    status: AutomationRunStatus,
    error_detail: str | None = None,
    max_duration: timedelta = DEFAULT_MAX_RUN_DURATION,
) -> None:
    """Update a run's status and set the appropriate timestamp.

    Sets started_at + timeout_at when transitioning to RUNNING, or
    completed_at when transitioning to COMPLETED or FAILED. Caller is
    responsible for committing the transaction.

    Args:
        session: Database session
        run: The run to update
        status: The new status to set
        error_detail: Optional error message (only used for FAILED status)
        max_duration: Maximum run duration for computing timeout_at
    """
    now = utcnow()

    values: dict = {"status": status}
    if status == AutomationRunStatus.RUNNING:
        values["started_at"] = now
        values["timeout_at"] = now + max_duration
        run.started_at = now
        run.timeout_at = now + max_duration
    elif status in (AutomationRunStatus.COMPLETED, AutomationRunStatus.FAILED):
        values["completed_at"] = now
        run.completed_at = now

    if error_detail and status == AutomationRunStatus.FAILED:
        values["error_detail"] = error_detail
        run.error_detail = error_detail

    await session.execute(
        update(AutomationRun).where(AutomationRun.id == run.id).values(**values)
    )

    run.status = status
