"""Automation run utilities."""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.db import using_sqlite
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.models import (
    Automation,
    AutomationDisableEvent,
    AutomationRun,
    AutomationRunStatus,
    AutomationState,
)
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.utils.change_events import (
    SYSTEM_ACTOR,
    record_core_disable,
    set_audit_actor,
)
from openhands.automation.utils.time import utcnow
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds


logger = logging.getLogger(__name__)

# Terminal statuses that count as a first-run outcome. CANCELLED and SKIPPED
# are excluded so a later real run still records the one outcome.
FIRST_RUN_OUTCOME_STATUSES = (
    AutomationRunStatus.COMPLETED,
    AutomationRunStatus.FAILED,
)


class DeferredRunRequeueError(RuntimeError):
    """The requeue write for a limit-blocked run failed.

    Raised instead of returning False: a caller that cannot tell a write
    failure from a rejected conditional update would treat it as a lost
    terminal race and return, leaving the run stranded as RUNNING with no
    sandbox until the watchdog reaps it. The caller lets this propagate so
    ``_execute_run_safe`` records the failure and the run reaches a terminal
    state immediately.
    """


async def disable_automation(
    session_factory: async_sessionmaker[AsyncSession],
    automation_id: uuid.UUID,
    reason: str,
    *,
    disabled_detail: dict | None = None,
    run_id: uuid.UUID | None = None,
    source: str = "permanent_dispatch_failure",
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
            set_audit_actor(session, SYSTEM_ACTOR)
            disabled_at = utcnow()
            # Use optimistic locking: only update if currently enabled
            result: CursorResult = await session.execute(  # type: ignore[assignment]
                update(Automation)
                .where(
                    Automation.id == automation_id,
                    Automation.enabled == True,  # noqa: E712
                )
                .values(
                    enabled=False,
                    state=AutomationState.INACTIVE,
                    disabled_reason=reason,
                    disabled_detail=disabled_detail,
                    disabled_at=disabled_at,
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

            await skip_pending_runs_for_disabled_automation(
                session,
                automation_id,
                reason=reason,
                disabled_detail=disabled_detail,
                completed_at=disabled_at,
            )

            automation = await session.get(Automation, automation_id)
            if automation is not None:
                await mark_git_sync_dirty(session, automation)
                # The Core update above is invisible to the audit hook.
                record_core_disable(session, automation)

            session.add(
                AutomationDisableEvent(
                    automation_id=automation_id,
                    run_id=run_id,
                    reason=reason,
                    detail=disabled_detail,
                    source=source,
                )
            )
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


async def skip_pending_runs_for_disabled_automation(
    session: AsyncSession,
    automation_id: uuid.UUID,
    *,
    reason: str,
    disabled_detail: dict | None = None,
    completed_at: datetime | None = None,
    include_manual: bool = False,
) -> int:
    """Mark accepted-but-not-dispatched runs terminal when automation is disabled."""
    completed_at = completed_at or utcnow()
    status_detail: dict = {
        "phase": "dispatch",
        "kind": "blocked",
        "detail": reason,
        "transient": False,
        "source": "automation_service",
        "operation": "automation_disabled",
        "user_action": "settings",
    }
    if disabled_detail is not None:
        status_detail["disabled_detail"] = disabled_detail

    filters = [
        AutomationRun.automation_id == automation_id,
        AutomationRun.status == AutomationRunStatus.PENDING,
    ]
    if not include_manual:
        filters.append(
            (AutomationRun.trigger_source.is_(None))
            | (AutomationRun.trigger_source != "manual")
        )

    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(AutomationRun)
        .where(*filters)
        .values(
            status=AutomationRunStatus.SKIPPED,
            completed_at=completed_at,
            error_detail="Automation disabled",
            status_detail=status_detail,
        )
    )
    return result.rowcount or 0


async def create_pending_run(
    session: AsyncSession,
    automation: Automation,
    *,
    telemetry_distinct_id: str | None = None,
    trigger_source: str | None = None,
    triggered_by_user_id: uuid.UUID | None = None,
    event_payload: dict[str, Any] | None = None,
    observability_parent_span_context: str | None = None,
) -> AutomationRun:
    """Create a PENDING automation run for dispatch.

    Also updates the automation's last_triggered_at and last_polled_at
    timestamps. Caller is responsible for committing the transaction.

    Args:
        session: Database session
        automation: The automation to create a run for
        triggered_by_user_id: The person who started a manual run; None for
            cron and event runs.
        event_payload: Optional synthetic event payload for manual test
            dispatches of event-triggered automations. Bypasses webhook
            signature verification because the caller is authenticated.

    Returns:
        The created AutomationRun
    """
    now = utcnow()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.PENDING,
        trigger_source=trigger_source,
        triggered_by_user_id=triggered_by_user_id,
        event_payload=event_payload,
        telemetry_distinct_id=(
            telemetry_distinct_id or automation.telemetry_distinct_id
        ),
        observability_parent_span_context=observability_parent_span_context,
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

    # Clear the deferral marker once a run is claimed: the dispatcher's poll
    # skips PENDING runs whose deferred_until is in the future, and a claimed
    # run must be immediately eligible again if it is later deferred anew.
    if status == AutomationRunStatus.RUNNING:
        values["deferred_until"] = None
        run.deferred_until = None

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


async def update_run_resolved_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    resolved_runtime: dict[str, Any],
) -> None:
    """Persist runtime attributes resolved during dispatch."""
    try:
        async with session_factory() as session:
            await session.execute(
                update(AutomationRun)
                .where(AutomationRun.id == run_id)
                .values(resolved_runtime=resolved_runtime)
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to update resolved_runtime for run %s", run_id)


async def update_run_timeout_at(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    timeout_at: datetime,
) -> bool:
    """Reset the watchdog deadline for a run that is still RUNNING.

    Guarded by status == RUNNING so a run that already reached a terminal
    state (callback, cancel, dispatch failure) never gets a deadline
    resurrected. Failure is non-fatal: the run keeps its previous (longer)
    provisioning-phase deadline, which errs toward a later watchdog check,
    never an earlier one.

    Returns False only when the run is known to be no longer RUNNING.
    """
    try:
        async with session_factory() as session:
            result: CursorResult = await session.execute(  # type: ignore[assignment]
                update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(timeout_at=timeout_at)
            )
            await session.commit()
            return result.rowcount != 0
    except Exception:
        logger.exception("Failed to update timeout_at for run %s", run_id)
        return True


async def run_is_running(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> bool:
    """Whether the run is still RUNNING (True if the check itself fails)."""
    try:
        async with session_factory() as session:
            status = await session.scalar(
                select(AutomationRun.status).where(AutomationRun.id == run_id)
            )
            return status == AutomationRunStatus.RUNNING
    except Exception:
        logger.exception("Failed to read status for run %s", run_id)
        return True


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


async def count_running_runs(session: AsyncSession) -> int:
    """Count runs currently RUNNING across the service.

    Backs the optional dispatch capacity gate. Only RUNNING counts: PENDING
    runs are the queue waiting for a slot, so counting them would deadlock the
    gate against itself.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(AutomationRun)
            .where(AutomationRun.status == AutomationRunStatus.RUNNING)
        )
        or 0
    )


async def requeue_deferred_run(
    session_factory: async_sessionmaker[AsyncSession],
    run: AutomationRun,
    *,
    deferred_until: datetime,
    status_detail: dict | None = None,
) -> bool:
    """Return a claimed run blocked by a transient limit to the PENDING queue.

    Guarded by status == RUNNING and clears started_at/timeout_at/completed_at,
    so a run that already reached a terminal state (callback, cancel, watchdog)
    is never resurrected, and requeueing cannot double-execute a run. Stamps
    deferred_until so the dispatch poll skips it until the backoff elapses.

    Returns True when the run was requeued and False when the guarded update
    matched no row (the run left RUNNING). A write failure raises
    ``DeferredRunRequeueError`` rather than returning False, so the caller can
    tell a lost race from an unreachable database and fail the run instead of
    leaving it stranded as RUNNING.
    """
    try:
        async with session_factory() as session:
            set_audit_actor(session, SYSTEM_ACTOR)
            result: CursorResult = await session.execute(  # type: ignore[assignment]
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.PENDING,
                    started_at=None,
                    timeout_at=None,
                    completed_at=None,
                    deferred_until=deferred_until,
                    deferral_count=AutomationRun.deferral_count + 1,
                    status_detail=status_detail,
                    current_phase=None,
                )
            )
            await session.commit()
            requeued = result.rowcount != 0
            if requeued:
                run.status = AutomationRunStatus.PENDING
                run.deferred_until = deferred_until
                run.deferral_count = (run.deferral_count or 0) + 1
                run.status_detail = status_detail
            return requeued
    except Exception as exc:
        logger.exception("Failed to requeue deferred run %s", run.id)
        raise DeferredRunRequeueError(
            f"Could not requeue run {run.id} after a transient limit"
        ) from exc


async def expire_aged_deferred_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_age_seconds: int,
    status_detail: dict,
    limit: int = 100,
) -> int:
    """Terminally SKIP deferred runs whose max deferral age has passed.

    Runs independently of admission capacity. A run deferred by a transient
    limit is normally expired by ``_defer_or_skip_blocked_run`` on its next
    dispatch attempt, but a poll that finds every slot occupied returns before
    polling, so a deferred run can sit PENDING indefinitely behind a full cap.
    This sweep reaps those rows on every poll regardless of available capacity.

    Selects PENDING runs whose retry date and max age have both passed and
    updates them in one guarded statement, so a run cancelled or claimed
    concurrently is left alone. Uses FOR UPDATE SKIP LOCKED on PostgreSQL so
    competing replicas never double-skip; SQLite runs single-process. Returns
    the number of runs skipped.
    """
    deadline = utcnow() - timedelta(seconds=max_age_seconds)
    now = utcnow()
    async with session_factory() as session:
        set_audit_actor(session, SYSTEM_ACTOR)
        select_query = (
            select(AutomationRun.id)
            .where(
                AutomationRun.status == AutomationRunStatus.PENDING,
                AutomationRun.deferred_until.is_not(None),
                AutomationRun.deferred_until <= now,
                AutomationRun.created_at <= deadline,
            )
            .order_by(AutomationRun.created_at.asc())
            .limit(limit)
        )
        if not using_sqlite():
            select_query = select_query.with_for_update(skip_locked=True)
        run_ids = list((await session.execute(select_query)).scalars().all())
        if not run_ids:
            return 0
        result: CursorResult = await session.execute(  # type: ignore[assignment]
            update(AutomationRun)
            .where(
                AutomationRun.id.in_(run_ids),
                AutomationRun.status == AutomationRunStatus.PENDING,
            )
            .values(
                status=AutomationRunStatus.SKIPPED,
                completed_at=now,
                deferred_until=None,
                status_detail=status_detail,
            )
        )
        await session.commit()
        skipped = result.rowcount or 0
    if skipped:
        logger.warning("Expired %d deferred run(s) past max deferral age", skipped)
    return skipped


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
                # Only background stages pass a factory; the callback passes its
                # own request session, whose actor is the run's key.
                set_audit_actor(local_session, SYSTEM_ACTOR)
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
            set_audit_actor(session, SYSTEM_ACTOR)
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
