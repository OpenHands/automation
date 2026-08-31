"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline. Before marking as FAILED, attempts to verify the
actual run status by querying the execution environment. A verification
result that means "the bash command may still be executing" defers the
deadline (bounded by a hard cap) instead of terminalizing the run.

The ``timeout_at`` column is set to a provisioning-phase deadline when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``), then
reset to bash-start + run budget + margin once the bash command starts
(see ``update_run_timeout_at``).

The watchdog is mode-agnostic — all mode-specific logic is encapsulated
in the ExecutionBackend (see automation/backends/).

The same loop prunes ``integration_events``: it is the service's only periodic
janitor, and a second loop buys nothing for one bounded DELETE.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from openhands.automation.backends import get_backend
from openhands.automation.config import Settings, get_config
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    IntegrationEvent,
)
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.utils import log_extra
from openhands.automation.utils.agent_server import VerificationOutcome
from openhands.automation.utils.run import record_first_run_outcome
from openhands.automation.utils.run_status_detail import (
    RunStatusDetailKind,
    RunStatusPhase,
    make_run_status_detail,
    run_status_detail_from_exception,
    run_status_detail_from_transient_error,
)
from openhands.automation.utils.time import ensure_utc, utcnow
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds


logger = logging.getLogger("automation.watchdog")

# Most rows a single prune deletes. Caps how long one statement holds locks on
# a table the receive path writes to; the loop runs again a minute later, so a
# backlog drains rather than being dropped.
PRUNE_BATCH_SIZE = 5000


async def _get_automation_keep_alive(
    session: AsyncSession, run: AutomationRun
) -> bool | None:
    """Return parent automation keep_alive without extra SQL when preloaded."""
    if "automation" not in inspect(run).unloaded and run.automation is not None:
        return run.automation.keep_alive

    return await session.scalar(
        select(Automation.keep_alive).where(Automation.id == run.automation_id)
    )


async def _get_automation_timeout(
    session: AsyncSession, run: AutomationRun
) -> int | None:
    """Return parent automation timeout without extra SQL when preloaded."""
    if "automation" not in inspect(run).unloaded and run.automation is not None:
        return run.automation.timeout

    return await session.scalar(
        select(Automation.timeout).where(Automation.id == run.automation_id)
    )


async def _defer_still_running(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
    now: datetime,
) -> bool:
    """Push timeout_at forward while the bash command may still be running.

    The bash command's own timeout (enforced by the agent-server from bash
    start) governs how long the run may execute; a still-running verification
    result at the watchdog deadline means only that the two clocks are
    skewed, not that the run failed. Deferral is bounded by a hard cap so a
    broken bash service cannot defer forever.

    Returns True when the run should be left non-terminal this scan
    (deferred, or already terminal via a racing callback/cancel). Returns
    False when the hard cap is exhausted and the caller must proceed to the
    terminal path.
    """
    extra = log_extra(run_id=str(run.id), sandbox_id=run.sandbox_id)
    sandbox_cfg = get_config().sandbox
    effective_timeout = resolve_automation_timeout_seconds(
        await _get_automation_timeout(session, run)
    )
    anchor = ensure_utc(run.started_at or run.created_at)
    hard_deadline = anchor + timedelta(
        seconds=sandbox_cfg.sandbox_ready_timeout
        + effective_timeout
        + sandbox_cfg.run_timeout_hard_grace
    )
    if now >= hard_deadline:
        return False

    new_timeout_at = min(
        now + timedelta(seconds=settings.watchdog_interval_seconds), hard_deadline
    )
    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(AutomationRun)
        .where(
            AutomationRun.id == run.id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(timeout_at=new_timeout_at, status_detail=None)
    )
    if result.rowcount > 0:
        logger.info(
            "Run still executing; deferring timeout_at to %s (hard cap %s)",
            new_timeout_at,
            hard_deadline,
            extra=extra,
        )
    # rowcount == 0 means a callback/cancel landed concurrently — also
    # non-terminal for the watchdog.
    return True


def _loaded_automation(run: AutomationRun) -> Automation | None:
    """Return the parent automation only when it is already loaded."""
    if "automation" in inspect(run).unloaded:
        return None
    return run.automation


def _should_cleanup_sandbox_after_terminal(
    run: AutomationRun, keep_alive: bool | None
) -> bool:
    """Return whether watchdog should explicitly delete this run's sandbox.

    A run owning a subject is held like `keep_alive`, exactly as `complete_run`
    holds it: the sandbox carries the conversation the next event continues,
    and a lost callback is the ordinary reason we get here. The runtime TTL
    reaper still collects it.
    """
    return bool(run.sandbox_id) and keep_alive is not True and not run.subject_key


async def _verify_and_mark_run(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
) -> bool:
    """Verify run status via backend and mark accordingly.

    Mode-agnostic: all verification logic is encapsulated in the backend.

    Returns True if the run was marked with a terminal status.
    """
    run_id = str(run.id)
    sandbox_id = run.sandbox_id
    extra = log_extra(run_id=run_id, sandbox_id=sandbox_id)
    now = utcnow()

    # Get backend for this run (mode-specific logic encapsulated)
    backend = get_backend(run)

    # Verify run status via backend
    try:
        logger.info("Verifying run status via backend", extra=extra)
        verification = await backend.verify_run(run_id)
    except Exception as e:
        logger.warning("Failed to verify run: %s", e, extra=extra)
        stmt = (
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(
                status=AutomationRunStatus.FAILED,
                completed_at=now,
                error_detail=f"Timed out: verification failed: {e}",
                status_detail=run_status_detail_from_exception(
                    e,
                    phase=RunStatusPhase.VERIFICATION,
                    source="automation_service",
                    operation="verify_run",
                    previous=run.status_detail,
                ),
            )
        )
        result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
        if result.rowcount > 0:
            await capture_automation_event(
                "automation_run_failed",
                automation=_loaded_automation(run),
                run=run,
                session=session,
                properties={
                    "trigger_source": "watchdog",
                    "failure_kind": "verification_failed",
                },
            )
            await record_first_run_outcome(
                run, AutomationRunStatus.FAILED, "watchdog", session=session
            )
        return result.rowcount > 0

    if verification.outcome in (
        VerificationOutcome.COMPLETED,
        VerificationOutcome.FAILED,
    ):
        exit_code = verification.exit_code

        # Command completed successfully; the callback was missed.
        if verification.outcome == VerificationOutcome.COMPLETED:
            logger.info(
                "Verified run completed successfully (exit_code=%s), "
                "callback was missed",
                exit_code,
                extra=extra,
            )
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.COMPLETED,
                    completed_at=now,
                    status_detail=None,
                )
            )

        # exit_code == -1 or None: Command was killed/timed out by bash service
        elif exit_code is None or exit_code == -1:
            error_msg = "command timed out or was killed"
            if verification.stderr:
                error_msg += f"\nstderr: {verification.stderr[-1000:]}"

            logger.warning(
                "Run timed out (exit_code=%s)",
                exit_code,
                extra=extra,
            )
            error_detail = f"Timed out: {error_msg}"
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.FAILED,
                    completed_at=now,
                    error_detail=error_detail,
                    status_detail=make_run_status_detail(
                        phase=RunStatusPhase.EXECUTION,
                        kind=RunStatusDetailKind.TIMEOUT,
                        detail=error_msg,
                        transient=False,
                        source="agent_server",
                        operation="verify_run",
                        code=str(exit_code) if exit_code is not None else None,
                        previous=run.status_detail,
                        extra={"formatted_detail": error_detail},
                    ),
                )
            )

        # Any other exit code: Command failed with an actual error
        else:
            error_parts = [f"exit_code={exit_code}"]
            if verification.stderr:
                error_parts.append(f"stderr: {verification.stderr[-1000:]}")
            if verification.stdout:
                error_parts.append(f"stdout: {verification.stdout[-500:]}")
            error_detail = "\n".join(error_parts)

            logger.warning(
                "Verified run failed (exit_code=%s)",
                exit_code,
                extra=extra,
            )
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.FAILED,
                    completed_at=now,
                    error_detail=error_detail,
                    status_detail=make_run_status_detail(
                        phase=RunStatusPhase.EXECUTION,
                        kind=RunStatusDetailKind.EXECUTION_ERROR,
                        detail=error_detail,
                        transient=False,
                        source="agent_server",
                        operation="verify_run",
                        code=str(exit_code),
                        previous=run.status_detail,
                    ),
                )
            )

        result = await session.execute(stmt)  # type: ignore[assignment]
        if result.rowcount > 0:
            await capture_automation_event(
                "automation_run_completed"
                if exit_code == 0
                else "automation_run_failed",
                automation=_loaded_automation(run),
                run=run,
                session=session,
                properties={
                    "trigger_source": "watchdog",
                    "verification_exit_code": exit_code,
                },
            )
            await record_first_run_outcome(
                run,
                AutomationRunStatus.COMPLETED
                if exit_code == 0
                else AutomationRunStatus.FAILED,
                "watchdog",
                session=session,
            )
        if result.rowcount > 0:
            keep_alive = await _get_automation_keep_alive(session, run)
            if _should_cleanup_sandbox_after_terminal(run, keep_alive):
                try:
                    await backend.cleanup_after_verification(run_id)
                except Exception as e:
                    logger.warning(
                        "Cleanup after terminal verification failed: %s",
                        e,
                        extra=extra,
                    )
        return result.rowcount > 0

    # Verification failed - execution environment not available or command still running
    if verification.outcome == VerificationOutcome.TRANSIENT_ERROR:
        logger.warning(
            "Verification temporarily unavailable; leaving run RUNNING: %s",
            verification.detail,
            extra=extra,
        )
        if verification.error_info is not None:
            status_detail = run_status_detail_from_transient_error(
                verification.error_info,
                phase=RunStatusPhase.VERIFICATION,
                previous=run.status_detail,
            )
        else:
            status_detail = make_run_status_detail(
                phase=RunStatusPhase.VERIFICATION,
                kind=RunStatusDetailKind.UNKNOWN,
                detail=verification.detail or "Verification temporarily unavailable",
                transient=True,
                source="automation_service",
                operation="verify_run",
                previous=run.status_detail,
            )
        await session.execute(
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(status_detail=status_detail)
        )
        return False

    # A still-running bash command is not a failure: its own timeout
    # (enforced by the agent-server) has not fired yet, so defer instead of
    # destroying a live run. Must happen before any cleanup below.
    if verification.outcome == VerificationOutcome.STILL_RUNNING:
        if await _defer_still_running(session, run, settings, now):
            return False
        logger.warning(
            "Still-running grace exhausted, proceeding to terminal timeout",
            extra=extra,
        )

    # This likely means the sandbox crashed, was cleaned up, or verification
    # failed in a way that is not known to be transient.
    logger.warning(
        "Could not verify run status: %s, marking as timed out",
        verification.detail,
        extra=extra,
    )

    # Clean up resources via backend only when the automation owns explicit
    # cleanup. Otherwise, leave cleanup to the runtime TTL reaper.
    keep_alive = await _get_automation_keep_alive(session, run)
    if _should_cleanup_sandbox_after_terminal(run, keep_alive):
        try:
            await backend.cleanup_after_verification(run_id)
        except Exception as e:
            logger.warning("Cleanup after verification failed: %s", e, extra=extra)

    error_msg = verification.detail or "no completion callback received"

    logger.warning(
        "Marking run as timed out: run_id=%s, sandbox_id=%s, timeout_at=%s, reason=%s",
        run_id,
        sandbox_id,
        run.timeout_at,
        error_msg,
        extra=extra,
    )

    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run.id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(
            status=AutomationRunStatus.FAILED,
            completed_at=now,
            error_detail=f"Timed out: {error_msg}",
            status_detail=make_run_status_detail(
                phase=RunStatusPhase.VERIFICATION,
                kind=(
                    RunStatusDetailKind.ENVIRONMENT_UNAVAILABLE
                    if verification.outcome
                    == VerificationOutcome.ENVIRONMENT_UNAVAILABLE
                    else RunStatusDetailKind.TIMEOUT
                ),
                detail=error_msg,
                transient=False,
                source="automation_service",
                operation="verify_run",
                previous=run.status_detail,
                extra={
                    "verification_outcome": (
                        verification.outcome.value
                        if verification.outcome
                        else "unknown"
                    )
                },
            ),
        )
    )
    result = await session.execute(stmt)  # type: ignore[assignment]
    if result.rowcount > 0:
        await capture_automation_event(
            "automation_run_failed",
            automation=_loaded_automation(run),
            run=run,
            session=session,
            properties={
                "trigger_source": "watchdog",
                "failure_kind": "timeout",
            },
        )
        await record_first_run_outcome(
            run, AutomationRunStatus.FAILED, "watchdog", session=session
        )
    return result.rowcount > 0


async def mark_stale_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Find and process stale RUNNING runs.

    A run is stale if ``timeout_at < now()``. Before marking as FAILED,
    attempts to verify the actual status by querying the sandbox. Uses
    optimistic locking so concurrent callbacks win.

    Each run is processed in its own session so that row locks are released
    immediately after commit rather than held for the duration of the batch.
    This prevents lock contention with concurrent callback UPDATEs.

    Returns the number of runs marked with terminal status.
    """
    now = utcnow()
    marked = 0

    async with session_factory() as session:
        # Fetch stale run IDs only — close this session before doing any
        # per-run work so we don't hold locks across slow verify calls.
        result = await session.execute(
            select(AutomationRun.id).where(
                AutomationRun.status == AutomationRunStatus.RUNNING,
                AutomationRun.timeout_at.isnot(None),
                AutomationRun.timeout_at < now,
            )
        )
        stale_run_ids = list(result.scalars().all())

    for run_id in stale_run_ids:
        async with session_factory() as session:
            # Re-fetch with automation relationship inside a fresh session.
            result = await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.id == run_id)
            )
            run = result.scalars().first()
            if run is None:
                continue

            extra = log_extra(run_id=str(run_id), sandbox_id=run.sandbox_id)

            logger.info(
                "Processing stale run (timeout_at=%s, now=%s)",
                run.timeout_at,
                now,
                extra=extra,
            )

            try:
                # Commit unconditionally: a non-terminal outcome may still
                # have deferred timeout_at, and that UPDATE must persist.
                terminal = await _verify_and_mark_run(session, run, settings)
                await session.commit()
                if terminal:
                    marked += 1
                else:
                    logger.info(
                        "Run not terminal (completed concurrently or deferred)",
                        extra=extra,
                    )
            except Exception:
                logger.exception("Error processing stale run", extra=extra)

    return marked


async def prune_integration_events(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Delete accepted events past the retention window, one batch per scan."""
    cutoff = utcnow() - timedelta(days=settings.integration_event_retention_days)

    async with session_factory() as session:
        result: CursorResult = await session.execute(  # type: ignore[assignment]
            delete(IntegrationEvent).where(
                IntegrationEvent.id.in_(
                    select(IntegrationEvent.id)
                    .where(IntegrationEvent.received_at < cutoff)
                    .limit(PRUNE_BATCH_SIZE)
                )
            )
        )
        await session.commit()

    return result.rowcount


async def watchdog_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main watchdog loop — scans for stale runs periodically.

    Args:
        session_factory: Async session maker for database access.
        settings: Application settings.
        shutdown_event: Event to signal graceful shutdown.
    """
    interval = settings.watchdog_interval_seconds

    logger.info(
        "Watchdog started, scanning every %ds",
        interval,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Watchdog received shutdown signal, exiting")
            break

        try:
            marked = await mark_stale_runs(session_factory, settings)
            if marked:
                logger.info("Processed %d stale run(s)", marked)
        except Exception:
            logger.exception("Error in watchdog scan")

        try:
            pruned = await prune_integration_events(session_factory, settings)
            if pruned:
                logger.info("Pruned %d expired integration event(s)", pruned)
        except Exception:
            logger.exception("Error pruning integration events")

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                logger.info("Watchdog received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
