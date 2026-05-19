"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline. Before marking as FAILED, attempts to verify the
actual run status by querying the execution environment.

The ``timeout_at`` column is set to ``started_at + max_duration`` when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``).

The watchdog is mode-agnostic — all mode-specific logic is encapsulated
in the ExecutionBackend (see automation/backends/).
"""

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from openhands.automation.backends import get_backend
from openhands.automation.config import Settings
from openhands.automation.models import AutomationRun, AutomationRunStatus
from openhands.automation.utils import log_extra
from openhands.automation.utils.time import utcnow


logger = logging.getLogger("automation.watchdog")


async def _verify_and_mark_run(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,  # noqa: ARG001 - kept for API compatibility
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
            )
        )
        result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    if verification.verified:
        exit_code = verification.exit_code

        # exit_code == 0: Command completed successfully, we just missed the callback
        if exit_code == 0:
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
                )
            )

        result = await session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    # Verification failed - execution environment not available or command still running
    # This likely means the sandbox crashed or was cleaned up
    logger.warning(
        "Could not verify run status: %s, marking as timed out",
        verification.error,
        extra=extra,
    )

    # Clean up resources via backend (Cloud deletes sandbox, local is no-op)
    # Skip cleanup if keep_alive is True — user wants to inspect the sandbox
    if not run.keep_alive:
        try:
            await backend.cleanup_after_verification(run_id)
        except Exception as e:
            logger.warning("Cleanup after verification failed: %s", e, extra=extra)

    error_msg = verification.error or "no completion callback received"

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
        )
    )
    result = await session.execute(stmt)  # type: ignore[assignment]
    return result.rowcount > 0


async def mark_stale_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Find and process stale RUNNING runs.

    A run is stale if ``timeout_at < now()`` (pre-computed at dispatch time).
    Before marking as FAILED, attempts to verify the actual status by querying
    the sandbox. Uses optimistic locking so concurrent callbacks win.

    Returns the number of runs marked with terminal status.
    """
    now = utcnow()
    marked = 0

    async with session_factory() as session:
        # Fetch stale runs with their automation relationship for API key access
        result = await session.execute(
            select(AutomationRun)
            .options(selectinload(AutomationRun.automation))
            .where(
                AutomationRun.status == AutomationRunStatus.RUNNING,
                AutomationRun.timeout_at.isnot(None),
                AutomationRun.timeout_at < now,
            )
        )
        stale_runs = result.scalars().all()

        for run in stale_runs:
            run_id = str(run.id)
            extra = log_extra(run_id=run_id, sandbox_id=run.sandbox_id)

            logger.info(
                "Processing stale run (timeout_at=%s, now=%s)",
                run.timeout_at,
                now,
                extra=extra,
            )

            try:
                if await _verify_and_mark_run(session, run, settings):
                    marked += 1
                else:
                    logger.info("Run already completed, skipping", extra=extra)
            except Exception:
                logger.exception("Error processing stale run", extra=extra)

        if marked:
            await session.commit()

    return marked


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

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                logger.info("Watchdog received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
