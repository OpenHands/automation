"""Automation run utilities."""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.db import using_sqlite
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.utils.time import utcnow
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds


logger = logging.getLogger(__name__)

# Terminal statuses that count as a first-run outcome. CANCELLED and SKIPPED
# are excluded so a later real run still records the one outcome.
FIRST_RUN_OUTCOME_STATUSES = (
    AutomationRunStatus.COMPLETED,
    AutomationRunStatus.FAILED,
)


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
    status_detail: dict | None = None,
    current_phase: str | None = None,
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
        status_detail: Optional structured lifecycle detail to persist
        current_phase: Optional live progress phase to persist. Unlike
            status_detail there is no clearing branch — the last phase is
            kept on terminal transitions by design.
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

    if status_detail is not None:
        values["status_detail"] = status_detail
        run.status_detail = status_detail
    elif status in (AutomationRunStatus.RUNNING, AutomationRunStatus.COMPLETED):
        values["status_detail"] = None
        run.status_detail = None

    if current_phase is not None:
        values["current_phase"] = current_phase
        run.current_phase = current_phase

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


async def update_run_timeout_at(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    timeout_at: datetime,
) -> None:
    """Reset the watchdog deadline for a run that is still RUNNING.

    Guarded by status == RUNNING so a run that already reached a terminal
    state (callback, cancel, dispatch failure) never gets a deadline
    resurrected. Failure is non-fatal: the run keeps its previous (longer)
    provisioning-phase deadline, which errs toward a later watchdog check,
    never an earlier one.
    """
    try:
        async with session_factory() as session:
            await session.execute(
                update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(timeout_at=timeout_at)
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to update timeout_at for run %s", run_id)


async def update_run_current_phase(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    phase: str,
) -> None:
    """Best-effort write of the live progress phase for an in-flight run.

    Guarded by status IN (PENDING, RUNNING) so a terminal run's final state
    is never disturbed. Failure is non-fatal — phases are cosmetic.
    """
    try:
        async with session_factory() as session:
            await session.execute(
                update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.status.in_(
                        (AutomationRunStatus.PENDING, AutomationRunStatus.RUNNING)
                    ),
                )
                .values(current_phase=phase)
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to update current_phase for run %s", run_id)


async def _record_first_run_outcome_in_session(
    session: AsyncSession,
    run: AutomationRun,
    status: AutomationRunStatus,
    stage: str,
) -> bool:
    """Write the first-run record if this automation still lacks one.

    Returns True when a record was written (caller commits).
    """
    query = select(Automation).where(Automation.id == run.automation_id)
    if not using_sqlite():
        # Serialize racing terminal transitions; SQLite writes serialize anyway.
        query = query.with_for_update()
    automation = (await session.execute(query)).scalars().first()
    if automation is None:
        return False

    metadata = automation.preset_metadata
    if not metadata or "template" not in metadata or "first_run" in metadata:
        return False

    outcome = "success" if status == AutomationRunStatus.COMPLETED else "failure"
    failure_stage = stage if status == AutomationRunStatus.FAILED else None
    # Reassign the whole dict: in-place mutation of a JSON column is not
    # change-tracked.
    automation.preset_metadata = {
        **metadata,
        "first_run": {
            "status": outcome,
            "failure_stage": failure_stage,
            "template_version": metadata["template"].get("version"),
            "recorded_at": utcnow().isoformat(),
        },
    }
    await session.flush()

    await capture_automation_event(
        "automation_template_first_run",
        automation=automation,
        run=run,
        session=session,
        properties={
            "template_id": metadata["template"].get("id"),
            "template_version": metadata["template"].get("version"),
            "outcome": outcome,
            "failure_stage": failure_stage,
        },
    )
    return True


async def record_first_run_outcome(
    run: AutomationRun,
    status: AutomationRunStatus,
    stage: str,
    *,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Record the one-time first terminal outcome of a template-created automation.

    Writes a non-sensitive record (no prompt, no error text) under
    ``preset_metadata["first_run"]`` and emits one telemetry event. The absence
    of that key is the once-guard; only automations carrying template
    provenance record an outcome. Never raises — the run lifecycle must not be
    affected.

    Args:
        run: The run that reached a terminal status.
        status: The terminal status (only COMPLETED/FAILED record an outcome).
        stage: Lifecycle stage that produced the outcome:
            "dispatch", "execution", or "watchdog".
        session: Session to record in; the caller commits.
        session_factory: Opens (and commits) a dedicated session when no
            session is given.
    """
    if status not in FIRST_RUN_OUTCOME_STATUSES:
        return

    try:
        if session is not None:
            await _record_first_run_outcome_in_session(session, run, status, stage)
        elif session_factory is not None:
            async with session_factory() as local_session:
                recorded = await _record_first_run_outcome_in_session(
                    local_session, run, status, stage
                )
                if recorded:
                    await local_session.commit()
    except Exception:
        logger.exception(
            "Failed to record first-run outcome",
            extra={"run_id": str(run.id), "automation_id": str(run.automation_id)},
        )


async def mark_run_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    run: AutomationRun,
    status: AutomationRunStatus,
    error: str | None = None,
    status_detail: dict | None = None,
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
        status_detail: Optional structured lifecycle detail to persist
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
                    status_detail=status_detail,
                )
                await session.commit()
                logger.info("Run marked as %s", status.value, extra=extra)
                # Dedicated session so a recording failure cannot affect the
                # already-committed run transition.
                await record_first_run_outcome(
                    db_run, status, "dispatch", session_factory=session_factory
                )
            else:
                logger.info(
                    "Run not marked %s (current status: %s)",
                    status.value,
                    db_run.status.value if db_run else "not found",
                    extra=extra,
                )
    except Exception:
        logger.exception("Failed to mark run as %s", status.value, extra=extra)
