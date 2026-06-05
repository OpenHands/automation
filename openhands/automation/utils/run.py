"""Automation run utilities."""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.config import get_config
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.time import utcnow


logger = logging.getLogger(__name__)


async def disable_automation(
    session_factory: async_sessionmaker[AsyncSession],
    automation_id: uuid.UUID,
    reason: str,
) -> bool:
    """Disable an automation due to a permanent configuration error.

    This function sets enabled=False on the automation when we detect
    an unrecoverable error condition (e.g., tarball URL doesn't exist).
    The automation can be re-enabled manually after fixing the configuration.

    Uses optimistic locking (UPDATE WHERE enabled=True) to handle race
    conditions when multiple runs fail simultaneously.

    Args:
        session_factory: Async session factory
        automation_id: The automation ID to disable
        reason: Human-readable reason for disabling (logged)

    Returns:
        True if the automation was disabled, False if not found or already disabled
    """
    extra = {"automation_id": str(automation_id)}

    try:
        async with session_factory() as session:
            # Use optimistic locking: only update if currently enabled
            result: CursorResult = await session.execute(  # type: ignore[assignment]
                update(Automation)
                .where(
                    Automation.id == automation_id,
                    Automation.enabled == True,  # noqa: E712
                )
                .values(enabled=False)
            )

            if result.rowcount == 0:
                # Either not found or already disabled - check which
                check = await session.execute(
                    select(Automation).where(Automation.id == automation_id)
                )
                if check.scalars().first() is None:
                    logger.warning("Cannot disable automation: not found", extra=extra)
                else:
                    logger.info("Automation already disabled", extra=extra)
                return False

            await session.commit()

            logger.warning(
                "Automation disabled due to permanent error: %s",
                reason,
                extra=extra,
            )
            return True

    except Exception:
        logger.exception("Failed to disable automation", extra=extra)
        return False


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
    max_duration: timedelta | None = None,
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
    if max_duration is None:
        max_duration = timedelta(seconds=get_config().sandbox.max_run_duration)

    now = utcnow()

    values: dict = {"status": status}
    if status == AutomationRunStatus.RUNNING:
        values["started_at"] = now
        values["timeout_at"] = now + max_duration
        run.started_at = now
        run.timeout_at = now + max_duration
    elif status in (
        AutomationRunStatus.COMPLETED,
        AutomationRunStatus.FAILED,
        AutomationRunStatus.CANCELLED,
    ):
        values["completed_at"] = now
        run.completed_at = now

    if error_detail and status == AutomationRunStatus.FAILED:
        values["error_detail"] = error_detail
        run.error_detail = error_detail

    await session.execute(
        update(AutomationRun).where(AutomationRun.id == run.id).values(**values)
    )

    run.status = status


async def update_sandbox_id(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    sandbox_id: str,
) -> None:
    """Store the sandbox ID on the automation run for later verification.

    Args:
        session_factory: Async session factory
        run_id: The run ID to update
        sandbox_id: The sandbox ID to store
    """
    try:
        async with session_factory() as session:
            await session.execute(
                update(AutomationRun)
                .where(AutomationRun.id == run_id)
                .values(sandbox_id=sandbox_id)
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to update sandbox_id for run %s", run_id)


async def update_bash_command_id(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    bash_command_id: str,
) -> None:
    """Store the agent-server BashCommand id on the automation run.

    The verifier reads this back later to filter BashOutput events by exactly
    this command, so it doesn't pick up output from concurrent bash activity
    on a shared agent server. Failure to record this is non-fatal — the run
    will still execute — but verification may fall back to the (buggy)
    most-recent-output behavior, which is what we're trying to avoid.
    """
    try:
        async with session_factory() as session:
            await session.execute(
                update(AutomationRun)
                .where(AutomationRun.id == run_id)
                .values(bash_command_id=bash_command_id)
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to update bash_command_id for run %s", run_id)


async def mark_run_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    run: AutomationRun,
    status: AutomationRunStatus,
    error: str | None = None,
) -> None:
    """Mark a run with a terminal status (COMPLETED or FAILED) if still RUNNING.

    This is a safe wrapper around mark_run_status that:
    1. Opens a new session
    2. Re-fetches the run to check current status
    3. Only updates if the run is still RUNNING (avoids race conditions)
    4. Commits and handles errors gracefully

    Args:
        session_factory: Async session factory
        run: The run to update (used to get the ID)
        status: The terminal status to set (COMPLETED or FAILED)
        error: Optional error message (only used for FAILED status)
    """
    from sqlalchemy import select

    run_id = str(run.id)
    automation_id = str(run.automation_id) if run.automation_id else None
    extra = {"run_id": run_id}
    if automation_id:
        extra["automation_id"] = automation_id

    try:
        async with session_factory() as session:
            db_result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run.id)
            )
            db_run = db_result.scalars().first()
            if db_run and db_run.status == AutomationRunStatus.RUNNING:
                await mark_run_status(
                    session,
                    db_run,
                    status,
                    error_detail=error,
                )
                await session.commit()
                logger.info("Run marked as %s", status.value, extra=extra)
            else:
                logger.info(
                    "Run not marked %s (current status: %s)",
                    status.value,
                    db_run.status.value if db_run else "not found",
                    extra=extra,
                )
    except Exception:
        logger.exception("Failed to mark run as %s", status.value, extra=extra)
