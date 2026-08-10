"""Automation run utilities."""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from openhands.automation.config import get_config
from openhands.automation.health import (
    AutomationFailure,
    classify_exception,
    failure_from_callback,
    next_consecutive_failure_count,
    should_disable,
)
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.time import utcnow
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds
from openhands.sdk.event.error_classification import FailureKind


logger = logging.getLogger(__name__)


async def apply_terminal_health(
    session: AsyncSession,
    run: AutomationRun,
    status: AutomationRunStatus,
    *,
    error: str | None = None,
    failure_kind: FailureKind | str | None = None,
    blocking_reason: str | None = None,
    threshold: int | None = None,
) -> AutomationFailure:
    """Persist outcome metadata and update the parent automation health state.

    ``run.automation`` must be loaded by the caller. The parent row is updated
    in the same transaction as the terminal run, so a dashboard cannot observe
    a disabled automation without the run that explains it.
    """
    if (
        status == AutomationRunStatus.FAILED
        and failure_kind is None
        and not blocking_reason
    ):
        failure = classify_exception(RuntimeError(error or "automation failed"))
    else:
        failure = failure_from_callback(failure_kind, error, blocking_reason)
    has_failure_metadata = (
        status == AutomationRunStatus.FAILED
        or failure_kind is not None
        or blocking_reason is not None
    )
    run.failure_kind = failure.kind.value if has_failure_metadata else None
    run.blocking_reason = blocking_reason

    if status not in {
        AutomationRunStatus.COMPLETED,
        AutomationRunStatus.FAILED,
    }:
        return failure

    automation = run.automation
    if automation is None:
        raise ValueError("terminal health requires the run's automation")

    locked_result = await session.execute(
        select(Automation)
        .where(Automation.id == automation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_automation = locked_result.scalars().first()
    if locked_automation is not None:
        automation = locked_automation
        run.automation = automation

    is_blocked_completion = (
        status == AutomationRunStatus.COMPLETED
        and failure.kind is FailureKind.AGENT_ACTION
        and bool(blocking_reason)
    )
    is_failure = status == AutomationRunStatus.FAILED or is_blocked_completion

    if threshold is None:
        threshold = get_config().service.unhealthy_failure_threshold

    automation.consecutive_failure_count = next_consecutive_failure_count(
        automation.consecutive_failure_count if is_failure else 0,
        failure,
    )

    if should_disable(
        failure,
        consecutive_failures=automation.consecutive_failure_count,
        threshold=threshold,
    ):
        automation.enabled = False
        automation.disabled_reason = blocking_reason or error or failure.kind.value
        automation.disabled_failure_kind = failure.kind.value

    await session.flush()
    return failure


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
                .values(
                    enabled=False,
                    disabled_reason=reason,
                    disabled_failure_kind=FailureKind.CONFIG.value,
                )
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
    *,
    telemetry_distinct_id: str | None = None,
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
        telemetry_distinct_id=(
            telemetry_distinct_id or automation.telemetry_distinct_id
        ),
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
        max_duration = timedelta(seconds=resolve_automation_timeout_seconds(None))

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
        AutomationRunStatus.SKIPPED,
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


async def mark_run_terminal_in_session(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: AutomationRunStatus,
    *,
    error: str | None = None,
    failure_kind: FailureKind | str | None = None,
    blocking_reason: str | None = None,
    threshold: int | None = None,
) -> AutomationRun | None:
    """Atomically finish a RUNNING run and apply its health outcome."""
    values: dict = {"status": status, "completed_at": utcnow()}
    if error and status == AutomationRunStatus.FAILED:
        values["error_detail"] = error

    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(**values)
    )
    if result.rowcount == 0:
        return None

    db_result = await session.execute(
        select(AutomationRun)
        .options(selectinload(AutomationRun.automation))
        .where(AutomationRun.id == run_id)
    )
    db_run = db_result.scalars().first()
    if db_run is None:
        return None

    await apply_terminal_health(
        session,
        db_run,
        status,
        error=error,
        failure_kind=failure_kind,
        blocking_reason=blocking_reason,
        threshold=threshold,
    )
    return db_run


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
    *,
    failure_kind: FailureKind | str | None = None,
    blocking_reason: str | None = None,
    threshold: int | None = None,
) -> None:
    """Mark a run terminal through the shared health policy if still RUNNING.

    This is a safe wrapper around mark_run_terminal_in_session that:
    1. Opens a new session
    2. Re-fetches the run to check current status
    3. Only updates if the run is still RUNNING (avoids race conditions)
    4. Commits and handles errors gracefully

    Args:
        session_factory: Async session factory
        run: The run to update (used to get the ID)
        status: The terminal status to set (COMPLETED or FAILED)
        error: Optional error message (only used for FAILED status)
        failure_kind: Optional structured classification from the SDK or service
        blocking_reason: Optional agent-reported reason for a blocked outcome
        threshold: Optional override for the auto-disable threshold
    """
    run_id = str(run.id)
    automation_id = str(run.automation_id) if run.automation_id else None
    extra = {"run_id": run_id}
    if automation_id:
        extra["automation_id"] = automation_id

    try:
        async with session_factory() as session:
            db_run = await mark_run_terminal_in_session(
                session,
                run.id,
                status,
                error=error,
                failure_kind=failure_kind,
                blocking_reason=blocking_reason,
                threshold=threshold,
            )
            if db_run is not None:
                await session.commit()
                logger.info("Run marked as %s", status.value, extra=extra)
            else:
                logger.info(
                    "Run not marked %s (current status: %s)",
                    status.value,
                    "not running or not found",
                    extra=extra,
                )
    except Exception:
        logger.exception("Failed to mark run as %s", status.value, extra=extra)
