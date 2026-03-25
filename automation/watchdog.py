"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline. Before marking as FAILED, attempts to verify the
actual run status by querying the sandbox's bash command history.

The ``timeout_at`` column is set to ``started_at + max_duration`` when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``).
"""

import asyncio
import logging
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

# Default scan interval
WATCHDOG_INTERVAL_SECONDS = 120


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


async def _verify_and_mark_run(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
) -> bool:
    """Verify run status via sandbox and mark accordingly.

    Attempts to connect to the sandbox and check the last bash command's exit code.
    If verification succeeds, marks the run based on the actual result.
    If verification fails (sandbox unavailable), marks as FAILED with timeout error.

    Returns True if the run was marked with a terminal status.
    """
    run_id = str(run.id)
    sandbox_id = run.sandbox_id
    extra = _run_extra(run_id=run_id, sandbox_id=sandbox_id)
    now = utcnow()

    # If no sandbox_id, we can't verify - mark as failed
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
        stmt = (
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(
                status=AutomationRunStatus.FAILED,
                completed_at=now,
                error_detail=f"Timed out: could not get API key for verification: {e}",
            )
        )
        result = await session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    # Try to verify via sandbox
    verification = await verify_run_status(
        api_url=settings.openhands_api_base_url,
        api_key=api_key,
        sandbox_id=sandbox_id,
        keep_alive=run.keep_alive,
        run_id=run_id,
    )

    if verification.verified:
        # We got an actual result from the sandbox
        if verification.success:
            logger.info(
                "Verified run completed successfully (exit_code=%s)",
                verification.exit_code,
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
        else:
            error_parts = [f"exit_code={verification.exit_code}"]
            if verification.stderr:
                error_parts.append(f"stderr: {verification.stderr[-1000:]}")
            if verification.stdout:
                error_parts.append(f"stdout: {verification.stdout[-500:]}")
            error_detail = "\n".join(error_parts)

            logger.warning(
                "Verified run failed (exit_code=%s)",
                verification.exit_code,
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

    # Verification failed - sandbox not available or command still running
    # This likely means the sandbox crashed or was cleaned up
    logger.warning(
        "Could not verify run status: %s, marking FAILED",
        verification.error,
        extra=extra,
    )

    # Clean up sandbox if not keep_alive (best effort, may already be gone)
    if not run.keep_alive and sandbox_id:
        await cleanup_sandbox(
            api_url=settings.openhands_api_base_url,
            api_key=api_key,
            sandbox_id=sandbox_id,
            run_id=run_id,
        )

    error_msg = verification.error or "no completion callback received"
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


async def watchdog_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    interval_seconds: int = WATCHDOG_INTERVAL_SECONDS,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main watchdog loop — scans for stale runs periodically."""
    logger.info(
        "Watchdog started, scanning every %ds",
        interval_seconds,
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
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
                logger.info("Watchdog received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
