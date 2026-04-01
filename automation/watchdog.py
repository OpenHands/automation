"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline. Before marking as FAILED, attempts to verify the
actual run status by querying the sandbox's bash command history.

The ``timeout_at`` column is set to ``started_at + max_duration`` when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``).

Also handles delayed sandbox cleanup. When runs complete (via callback or
verification), a ``cleanup_at`` timestamp is set based on the configured
``sandbox_cleanup_delay_mins``. The cleanup scanner processes runs past
their cleanup deadline.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from automation.config import Settings
from automation.models import AutomationRun, AutomationRunStatus
from automation.utils.api_key import get_api_key_for_automation_run
from automation.utils.sandbox import cleanup_sandbox, verify_run_status
from automation.utils.time import utcnow


logger = logging.getLogger("automation.watchdog")


def _run_extra(
    run_id: str | None = None,
    sandbox_id: str | None = None,
) -> dict[str, Any]:
    """Build extra dict for structured logging."""
    extra: dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if sandbox_id:
        extra["sandbox_id"] = sandbox_id
    return extra


def _compute_cleanup_at(settings: Settings, now=None):
    """Compute cleanup_at timestamp based on settings.

    Returns None if delay is 0 (immediate cleanup), otherwise returns
    now + delay.
    """
    if now is None:
        now = utcnow()
    delay_mins = settings.sandbox_cleanup_delay_mins
    if delay_mins <= 0:
        return None
    return now + timedelta(minutes=delay_mins)


async def _verify_and_mark_run(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
) -> bool:
    """Verify run status via sandbox and mark accordingly.

    Attempts to connect to the sandbox and check the last bash command's exit code.
    If verification succeeds, marks the run based on the actual result.
    If verification fails (sandbox unavailable), marks as FAILED with timeout error.

    Sandbox cleanup is handled via the cleanup_at timestamp. If
    sandbox_cleanup_delay_mins > 0, cleanup is delayed. If 0, immediate
    cleanup is performed.

    Returns True if the run was marked with a terminal status.
    """
    run_id = str(run.id)
    sandbox_id = run.sandbox_id
    extra = _run_extra(run_id=run_id, sandbox_id=sandbox_id)
    now = utcnow()

    # If no sandbox_id, we can't verify - mark as failed (no cleanup needed)
    if not sandbox_id:
        logger.warning("No sandbox_id for stale run, marking FAILED", extra=extra)
        stmt = (
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(
                status=AutomationRunStatus.FAILED,
                completed_at=now,
                error_detail="Timed out: no sandbox_id available for verification",
            )
        )
        result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    # Get API key for sandbox access
    try:
        api_key = await get_api_key_for_automation_run(run)
    except Exception as e:
        logger.warning("Failed to get API key for verification: %s", e, extra=extra)
        # Can't cleanup without API key, just mark as failed
        # Schedule cleanup if delay is configured - but without API key, cleanup
        # will fail anyway; the sandbox may need manual cleanup or will be cleaned
        # up by other means (e.g., sandbox TTL)
        cleanup_at = _compute_cleanup_at(settings, now)
        values: dict = {
            "status": AutomationRunStatus.FAILED,
            "completed_at": now,
            "error_detail": f"Timed out: could not get API key for verification: {e}",
        }
        if cleanup_at and not run.keep_alive:
            values["cleanup_at"] = cleanup_at
        stmt = (
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(**values)
        )
        result = await session.execute(stmt)  # type: ignore[assignment]
        # Note: Can't cleanup sandbox without API key
        return result.rowcount > 0

    # Try to verify via sandbox - pass keep_alive=True to prevent deletion
    # (cleanup is handled by the cleanup scanner)
    verification = await verify_run_status(
        api_url=settings.openhands_api_base_url,
        api_key=api_key,
        sandbox_id=sandbox_id,
        keep_alive=True,  # Always prevent immediate deletion, use cleanup_at instead
        run_id=run_id,
    )

    # Compute cleanup_at for terminal states
    cleanup_at = _compute_cleanup_at(settings, now)

    if verification.verified:
        exit_code = verification.exit_code

        # Build base values for update
        base_values: dict = {
            "completed_at": now,
        }
        if cleanup_at and not run.keep_alive:
            base_values["cleanup_at"] = cleanup_at

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
                    **base_values,
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
                    error_detail=f"Timed out: {error_msg}",
                    **base_values,
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
                    error_detail=error_detail,
                    **base_values,
                )
            )

        result = await session.execute(stmt)  # type: ignore[assignment]

        # Immediate cleanup if delay is 0
        if not cleanup_at and not run.keep_alive:
            await cleanup_sandbox(
                api_url=settings.openhands_api_base_url,
                api_key=api_key,
                sandbox_id=sandbox_id,
                run_id=run_id,
            )

        return result.rowcount > 0

    # Verification failed - sandbox not available or command still running
    # This likely means the sandbox crashed or was cleaned up
    logger.warning(
        "Could not verify run status: %s, marking as timed out",
        verification.error,
        extra=extra,
    )

    error_msg = verification.error or "no completion callback received"

    logger.warning(
        "Marking run as timed out: run_id=%s, sandbox_id=%s, timeout_at=%s, reason=%s",
        run_id,
        sandbox_id,
        run.timeout_at,
        error_msg,
        extra=extra,
    )

    # Build values for failed state
    values = {
        "status": AutomationRunStatus.FAILED,
        "completed_at": now,
        "error_detail": f"Timed out: {error_msg}",
    }
    if cleanup_at and not run.keep_alive:
        values["cleanup_at"] = cleanup_at

    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run.id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(**values)
    )
    result = await session.execute(stmt)  # type: ignore[assignment]

    # Immediate cleanup if delay is 0 (best effort, sandbox may already be gone)
    if not cleanup_at and not run.keep_alive:
        await cleanup_sandbox(
            api_url=settings.openhands_api_base_url,
            api_key=api_key,
            sandbox_id=sandbox_id,
            run_id=run_id,
        )

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
            extra = _run_extra(run_id=run_id, sandbox_id=run.sandbox_id)

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


async def cleanup_pending_sandboxes(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Clean up sandboxes for completed runs that are past their cleanup_at deadline.

    Finds runs where:
    - cleanup_at < now (cleanup deadline has passed)
    - sandbox_id is not NULL (sandbox hasn't been cleaned up yet)
    - keep_alive is False (not marked for preservation)

    After deleting the sandbox, clears the sandbox_id to prevent duplicate
    cleanup attempts.

    Returns the number of sandboxes cleaned up.
    """
    now = utcnow()
    cleaned = 0

    async with session_factory() as session:
        # Fetch runs ready for cleanup
        result = await session.execute(
            select(AutomationRun)
            .options(selectinload(AutomationRun.automation))
            .where(
                AutomationRun.cleanup_at.isnot(None),
                AutomationRun.cleanup_at < now,
                AutomationRun.sandbox_id.isnot(None),
                AutomationRun.keep_alive == False,  # noqa: E712
            )
        )
        runs_to_cleanup = result.scalars().all()

        for run in runs_to_cleanup:
            run_id = str(run.id)
            sandbox_id = run.sandbox_id
            # sandbox_id is guaranteed non-None by the query filter above
            assert sandbox_id is not None
            extra = _run_extra(run_id=run_id, sandbox_id=sandbox_id)

            logger.info(
                "Cleaning up sandbox (cleanup_at=%s, now=%s)",
                run.cleanup_at,
                now,
                extra=extra,
            )

            try:
                # Get API key for sandbox deletion
                api_key = await get_api_key_for_automation_run(run)

                # Delete the sandbox
                deleted = await cleanup_sandbox(
                    api_url=settings.openhands_api_base_url,
                    api_key=api_key,
                    sandbox_id=sandbox_id,
                    run_id=run_id,
                )

                # Clear sandbox_id and cleanup_at to prevent duplicate cleanup
                stmt = (
                    update(AutomationRun)
                    .where(AutomationRun.id == run.id)
                    .values(sandbox_id=None, cleanup_at=None)
                )
                await session.execute(stmt)
                cleaned += 1

                if deleted:
                    logger.info("Sandbox cleaned up successfully", extra=extra)
                else:
                    logger.warning(
                        "Sandbox cleanup returned False (may already be gone)",
                        extra=extra,
                    )

            except Exception:
                logger.exception("Error cleaning up sandbox", extra=extra)

        if cleaned:
            await session.commit()

    return cleaned


async def watchdog_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main watchdog loop — scans for stale runs and cleanup periodically.

    Performs two tasks each interval:
    1. Marks stale RUNNING runs as FAILED (after verification)
    2. Cleans up sandboxes past their cleanup_at deadline

    Args:
        session_factory: Async session maker for database access.
        settings: Application settings.
        shutdown_event: Event to signal graceful shutdown.
    """
    interval = settings.watchdog_interval_seconds

    logger.info(
        "Watchdog started, scanning every %ds (cleanup_delay=%d mins)",
        interval,
        settings.sandbox_cleanup_delay_mins,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Watchdog received shutdown signal, exiting")
            break

        try:
            # Mark stale runs
            marked = await mark_stale_runs(session_factory, settings)
            if marked:
                logger.info("Processed %d stale run(s)", marked)

            # Clean up sandboxes past their cleanup deadline
            cleaned = await cleanup_pending_sandboxes(session_factory, settings)
            if cleaned:
                logger.info("Cleaned up %d sandbox(es)", cleaned)
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
