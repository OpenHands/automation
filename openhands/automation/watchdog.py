"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline. Before marking as FAILED, attempts to verify the
actual run status by querying the execution environment.

The ``timeout_at`` column is set to ``started_at + max_duration`` when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``).

Also handles delayed cleanup of cloud sandboxes. The watchdog is mode-agnostic
— all mode-specific logic is encapsulated in the ExecutionBackend (see
automation/backends/).
"""

import asyncio
import logging
from datetime import timedelta

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


def _compute_cleanup_at(settings: Settings, now=None):
    """Compute the delayed cleanup timestamp, or None for immediate cleanup."""
    if now is None:
        now = utcnow()
    delay_mins = settings.sandbox_cleanup_delay_mins
    if delay_mins <= 0:
        return None
    return now + timedelta(minutes=delay_mins)


async def _cleanup_now(run: AutomationRun, run_id: str, extra: dict) -> None:
    """Best-effort immediate cleanup via the run's execution backend."""
    if run.keep_alive:
        return
    try:
        await get_backend(run).cleanup_after_verification(run_id)
    except Exception as e:
        logger.warning("Cleanup after verification failed: %s", e, extra=extra)


async def _verify_and_mark_run(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
) -> bool:
    """Verify run status via backend and mark accordingly.

    Mode-agnostic: all verification logic is encapsulated in the backend.
    Cleanup is scheduled via ``cleanup_at`` when a positive cleanup delay is
    configured; otherwise it is performed immediately after the terminal status
    update wins the optimistic lock.

    Returns True if the run was marked with a terminal status.
    """
    run_id = str(run.id)
    sandbox_id = run.sandbox_id
    extra = log_extra(run_id=run_id, sandbox_id=sandbox_id)
    now = utcnow()
    cleanup_at = _compute_cleanup_at(settings, now)

    backend = get_backend(run)

    try:
        logger.info("Verifying run status via backend", extra=extra)
        verification = await backend.verify_run(run_id)
    except Exception as e:
        logger.warning("Failed to verify run: %s", e, extra=extra)
        values: dict = {
            "status": AutomationRunStatus.FAILED,
            "completed_at": now,
            "error_detail": f"Timed out: verification failed: {e}",
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
        result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
        if result.rowcount > 0 and cleanup_at is None:
            await _cleanup_now(run, run_id, extra)
        return result.rowcount > 0

    if verification.verified:
        exit_code = verification.exit_code
        base_values: dict = {"completed_at": now}
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
        if result.rowcount > 0 and cleanup_at is None:
            await _cleanup_now(run, run_id, extra)
        return result.rowcount > 0

    # Verification failed - execution environment not available or command still running
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

    values: dict = {
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
    if result.rowcount > 0 and cleanup_at is None:
        await _cleanup_now(run, run_id, extra)
    return result.rowcount > 0


async def mark_stale_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Find and process stale RUNNING runs.

    A run is stale if ``timeout_at < now()`` (pre-computed at dispatch time).
    Before marking as FAILED, attempts to verify the actual status by querying
    the sandbox. Uses optimistic locking so concurrent callbacks win.

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
                if await _verify_and_mark_run(session, run, settings):
                    await session.commit()
                    marked += 1
                else:
                    logger.info("Run already completed, skipping", extra=extra)
            except Exception:
                logger.exception("Error processing stale run", extra=extra)

    return marked


async def cleanup_pending_sandboxes(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,  # noqa: ARG001 - kept for watchdog API symmetry/tests
) -> int:
    """Clean up sandboxes for terminal runs past their cleanup deadline.

    Finds runs where ``cleanup_at`` has passed, ``sandbox_id`` is still set,
    and ``keep_alive`` is false. After successful backend cleanup, clears
    ``sandbox_id`` and ``cleanup_at`` to prevent duplicate cleanup attempts.

    Returns the number of runs cleaned up.
    """
    now = utcnow()
    cleaned = 0

    async with session_factory() as session:
        result = await session.execute(
            select(AutomationRun)
            .options(selectinload(AutomationRun.automation))
            .where(
                AutomationRun.status.in_(
                    [AutomationRunStatus.COMPLETED, AutomationRunStatus.FAILED]
                ),
                AutomationRun.cleanup_at.isnot(None),
                AutomationRun.cleanup_at < now,
                AutomationRun.sandbox_id.isnot(None),
                AutomationRun.keep_alive == False,  # noqa: E712
            )
        )
        runs_to_cleanup = result.scalars().all()

        for run in runs_to_cleanup:
            run_id = str(run.id)
            extra = log_extra(run_id=run_id, sandbox_id=run.sandbox_id)
            logger.info(
                "Cleaning up sandbox (cleanup_at=%s, now=%s)",
                run.cleanup_at,
                now,
                extra=extra,
            )

            try:
                await get_backend(run).cleanup_after_verification(run_id)
                stmt = (
                    update(AutomationRun)
                    .where(AutomationRun.id == run.id)
                    .values(sandbox_id=None, cleanup_at=None)
                )
                await session.execute(stmt)
                cleaned += 1
                logger.info("Sandbox cleanup completed", extra=extra)
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
    """Main watchdog loop — scans for stale runs and delayed cleanup."""
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
            marked = await mark_stale_runs(session_factory, settings)
            if marked:
                logger.info("Processed %d stale run(s)", marked)

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
