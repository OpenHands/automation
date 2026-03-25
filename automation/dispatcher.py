"""Dispatcher for processing pending automation runs.

Polls the automation_runs table for PENDING jobs and dispatches them
to sandboxes via the SaaS API.  Uses FOR UPDATE SKIP LOCKED for
multi-worker safety.

Completion is handled asynchronously: the SDK running inside the sandbox
POSTs to ``/api/v1/automations/runs/{id}/complete`` when the entry-point
exits, so the dispatcher does **not** block waiting for results.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from automation.config import Settings
from automation.execution import run_automation
from automation.models import AutomationRun, AutomationRunStatus, TarballUpload
from automation.utils.api_key import APIKeyError, get_api_key_for_automation_run
from automation.utils.run import mark_run_status
from automation.utils.tarball_validation import is_http_url, parse_internal_upload_id


logger = logging.getLogger("automation.dispatcher")


def _run_extra(
    run_id: str | None = None,
    automation_id: str | None = None,
    sandbox_id: str | None = None,
) -> dict[str, Any]:
    """Build extra dict for structured logging with run/automation/sandbox IDs."""
    extra: dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if automation_id:
        extra["automation_id"] = automation_id
    if sandbox_id:
        extra["sandbox_id"] = sandbox_id
    return extra


DEFAULT_BATCH_SIZE = 10
POLL_INTERVAL_SECONDS = 30


async def _download_internal_tarball(
    upload_id: uuid.UUID,
    session: AsyncSession | None,
) -> bytes:
    """Download a tarball from GCS using the TarballUpload record."""
    if session is None:
        raise ValueError("Database session required to resolve oh-internal:// URLs")

    result = await session.execute(
        select(TarballUpload).where(TarballUpload.id == upload_id)
    )
    upload = result.scalars().first()
    if upload is None:
        raise FileNotFoundError(f"TarballUpload {upload_id} not found")

    from automation.storage import GoogleCloudFileStore

    store = GoogleCloudFileStore()
    return store.read(upload.storage_path)


async def _poll_pending_runs(
    session: AsyncSession,
    batch_size: int,
) -> list[AutomationRun]:
    """Poll pending runs using FOR UPDATE SKIP LOCKED.

    Eagerly loads the ``automation`` relationship so that ``user_id``,
    ``org_id``, and tarball config are available for dispatch.
    """
    select_query = (
        select(AutomationRun)
        .options(selectinload(AutomationRun.automation))
        .where(AutomationRun.status == AutomationRunStatus.PENDING)
        .order_by(AutomationRun.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(select_query)
    return list(result.scalars().all())


async def _execute_run(
    run: AutomationRun,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Execute a single run in a background task.

    1. Fetch a per-user API key from the SaaS service (on demand, never stored).
    2. Determine tarball source:
       - Internal (oh-internal://): Download from GCS and upload to sandbox.
       - External (http/https): Pass URL for direct download inside sandbox.
    3. Call ``run_automation()`` to spin up a sandbox and execute.
    4. If the sandbox itself fails to start, mark the run FAILED.

    The SDK inside the sandbox fires the completion callback on exit,
    so we don't need to inspect the result for the happy path.
    """
    run_id = str(run.id)
    automation = run.automation
    automation_id = str(automation.id)
    tarball_path = automation.tarball_path

    # Helper for consistent structured logging
    def log_extra(sandbox_id: str | None = None) -> dict[str, Any]:
        return _run_extra(
            run_id=run_id, automation_id=automation_id, sandbox_id=sandbox_id
        )

    callback_url = (
        f"{settings.resolved_base_url.rstrip('/')}"
        f"/api/v1/automations/runs/{run_id}/complete"
    )

    try:
        # 1. Fetch a per-user API key from the SaaS service
        api_key = await get_api_key_for_automation_run(run)

        # 2. Determine tarball source
        tarball_source: bytes | str
        if is_http_url(tarball_path):
            # HTTP(S) URL: download directly inside sandbox (untrusted/large)
            tarball_source = tarball_path
            logger.info("HTTP URL tarball, will download in sandbox", extra=log_extra())
        else:
            # Internal (oh-internal://): download from GCS, upload to sandbox
            upload_id = parse_internal_upload_id(tarball_path)
            if upload_id is None:
                raise ValueError(f"Unsupported tarball_path: {tarball_path!r}")

            async with session_factory() as session:
                tarball_source = await _download_internal_tarball(upload_id, session)
            logger.info(
                "Internal tarball downloaded (%d bytes)",
                len(tarball_source),
                extra=log_extra(),
            )

        # 3. Build env vars for the sandbox
        env_vars = {
            "OPENHANDS_API_KEY": api_key,
            "OPENHANDS_CLOUD_API_URL": settings.openhands_api_base_url,
        }

        # Trigger context so the SDK script knows *why* it was invoked
        event_payload = {
            "trigger": automation.triggers,
            "automation_id": str(automation.id),
            "automation_name": automation.name,
        }
        env_vars["AUTOMATION_EVENT_PAYLOAD"] = json.dumps(event_payload)

        # 4. Launch the sandbox
        result = await run_automation(
            api_url=settings.openhands_api_base_url,
            api_key=api_key,
            entrypoint=automation.entrypoint,
            tarball_source=tarball_source,
            env_vars=env_vars,
            callback_url=callback_url,
            run_id=run_id,
        )

        sandbox_extra = log_extra(sandbox_id=result.sandbox_id)
        if result.success:
            # Mark the run as COMPLETED now that the entrypoint finished successfully.
            # Note: The SDK callback may also try to mark it COMPLETED, but we use
            # optimistic locking so the first one wins.
            logger.info("Marking run as COMPLETED", extra=sandbox_extra)
            await _mark_run_terminal(
                session_factory, run, AutomationRunStatus.COMPLETED
            )
        else:
            logger.warning(
                "Sandbox execution failed: %s",
                result.error,
                extra=sandbox_extra,
            )
            logger.warning(
                "Full output:\n--- STDOUT (last 2000 chars) ---\n%s\n"
                "--- STDERR (last 2000 chars) ---\n%s",
                result.stdout[-2000:] if result.stdout else "(empty)",
                result.stderr[-2000:] if result.stderr else "(empty)",
                extra=sandbox_extra,
            )
            await _mark_run_terminal(
                session_factory, run, AutomationRunStatus.FAILED, result.error
            )

    except (APIKeyError, ValueError) as exc:
        logger.error("Dispatch error: %s", exc, exc_info=True, extra=log_extra())
        await _mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, str(exc)
        )
    except Exception:
        logger.exception("Background execution failed", extra=log_extra())
        await _mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, "Internal error"
        )


async def _mark_run_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    run: AutomationRun,
    status: AutomationRunStatus,
    error: str | None = None,
) -> None:
    """Mark a run with a terminal status (COMPLETED or FAILED) if still RUNNING."""
    run_id = str(run.id)
    automation_id = str(run.automation_id) if run.automation_id else None
    extra = _run_extra(run_id=run_id, automation_id=automation_id)
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


async def dispatch_pending_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[AutomationRun]:
    """Poll for pending runs, mark RUNNING, and launch sandboxes.

    Each run is dispatched as an ``asyncio.create_task`` so the
    dispatcher loop is not blocked by long-running automations.
    """
    async with session_factory() as session:
        pending_runs = await _poll_pending_runs(session, batch_size)

        dispatched_runs = []
        for run in pending_runs:
            run_id = str(run.id)
            automation_id = str(run.automation_id) if run.automation_id else None
            extra = _run_extra(run_id=run_id, automation_id=automation_id)
            try:
                logger.info("Dispatching automation run", extra=extra)
                await mark_run_status(session, run, AutomationRunStatus.RUNNING)
                dispatched_runs.append(run)
            except Exception:
                logger.exception("Failed to dispatch run", extra=extra)

        await session.commit()

        for run in dispatched_runs:
            asyncio.create_task(
                _execute_run_safe(run, settings, session_factory),
                name=f"execute-run-{run.id}",
            )

        return dispatched_runs


async def _execute_run_safe(
    run: AutomationRun,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Wrapper around ``_execute_run`` that never lets exceptions escape.

    ``asyncio.create_task`` silently swallows exceptions from background
    tasks, so this wrapper ensures every failure is logged and the run is
    marked FAILED.
    """
    run_id = str(run.id)
    automation_id = str(run.automation_id) if run.automation_id else None
    extra = _run_extra(run_id=run_id, automation_id=automation_id)
    try:
        await _execute_run(run, settings, session_factory)
    except Exception:
        logger.exception("Background execution failed", extra=extra)
        await _mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, "Internal error"
        )


async def dispatcher_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    interval_seconds: int = POLL_INTERVAL_SECONDS,
    shutdown_event: asyncio.Event | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Main dispatcher loop — polls for pending runs and dispatches them."""
    logger.info(
        "Dispatcher started, polling every %d seconds (batch_size=%d)",
        interval_seconds,
        batch_size,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Dispatcher received shutdown signal, exiting")
            break

        try:
            dispatched = await dispatch_pending_runs(
                session_factory, settings=settings, batch_size=batch_size
            )
            if dispatched:
                logger.info("Dispatched %d run(s)", len(dispatched))
            else:
                logger.debug("No pending runs to dispatch")
        except Exception:
            logger.error("Error dispatching pending runs", exc_info=True)

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
                logger.info("Dispatcher received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
