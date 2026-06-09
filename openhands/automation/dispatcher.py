"""Dispatcher for processing pending automation runs.

Polls the automation_runs table for PENDING jobs and dispatches them
to execution backends (Cloud sandbox or local agent server).

Uses FOR UPDATE SKIP LOCKED for multi-worker safety (PostgreSQL).
SQLite deployments skip row locking (single-process mode assumed).

Completion is handled asynchronously: the SDK running inside the execution
environment POSTs to ``/v1/runs/{id}/complete`` when the entry-point
exits, so the dispatcher does **not** block waiting for results.

The dispatcher is mode-agnostic — all mode-specific logic is encapsulated
in the ExecutionBackend (see automation/backends/).
"""

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from openhands.automation.backends import get_backend
from openhands.automation.config import ServiceSettings, get_config
from openhands.automation.db import using_sqlite
from openhands.automation.exceptions import (
    ConcurrencyLimitReachedError,
    PermanentDispatchError,
    TarballNotFoundError,
)
from openhands.automation.execution import execute_in_context
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    TarballUpload,
)
from openhands.automation.utils import log_extra
from openhands.automation.utils.api_key import APIKeyError
from openhands.automation.utils.run import (
    disable_automation,
    mark_run_status,
    mark_run_terminal,
    update_bash_command_id,
    update_sandbox_id,
)
from openhands.automation.utils.tarball_validation import (
    is_http_url,
    parse_internal_upload_id,
)


logger = logging.getLogger("automation.dispatcher")


async def _download_internal_tarball(
    upload_id: uuid.UUID,
    session: AsyncSession | None,
) -> bytes:
    """Download a tarball from storage using the TarballUpload record.

    Raises:
        TarballNotFoundError: If the tarball upload record doesn't exist.
            This is a permanent error that should disable the automation.
        ValueError: If no database session is provided.
    """
    if session is None:
        raise ValueError("Database session required to resolve oh-internal:// URLs")

    result = await session.execute(
        select(TarballUpload).where(TarballUpload.id == upload_id)
    )
    upload = result.scalars().first()
    if upload is None:
        raise TarballNotFoundError(
            f"Internal tarball upload not found: {upload_id}. "
            "The tarball may have been deleted."
        )

    from openhands.automation.storage import get_file_store

    store = get_file_store()
    return store.read(upload.storage_path)


async def _poll_pending_runs(
    session: AsyncSession,
    batch_size: int,
) -> list[AutomationRun]:
    """Poll pending runs, optionally using FOR UPDATE SKIP LOCKED.

    For PostgreSQL: Uses FOR UPDATE SKIP LOCKED so multiple workers can poll
    concurrently without picking the same rows.

    For SQLite: Skips row locking (not supported). SQLite deployments assume
    single-process mode where row locking isn't needed.

    Eagerly loads the ``automation`` relationship so that ``user_id``,
    ``org_id``, and tarball config are available for dispatch.
    """
    select_query = (
        select(AutomationRun)
        .options(selectinload(AutomationRun.automation))
        .where(AutomationRun.status == AutomationRunStatus.PENDING)
        .order_by(AutomationRun.created_at.asc())
        .limit(batch_size)
    )

    # Apply row locking for PostgreSQL only (SQLite doesn't support it)
    if not using_sqlite():
        select_query = select_query.with_for_update(skip_locked=True)

    result = await session.execute(select_query)
    return list(result.scalars().all())


def _build_event_payload(
    automation: Automation,
    run: AutomationRun,
) -> dict[str, Any]:
    """Build the AUTOMATION_EVENT_PAYLOAD dict for an automation run.

    The ``trigger`` field is set to the trigger *type* string (e.g. ``"cron"``,
    ``"event"``) rather than the full trigger dict.  This keeps the value short
    enough for use as a conversation tag (max 256 chars).  The complete trigger
    configuration is preserved in ``trigger_payload`` so downstream code
    (including user-authored tarballs) can still access all trigger fields.

    See: https://github.com/OpenHands/automation/issues/111
    """
    trigger = automation.trigger or {}
    if isinstance(trigger, dict):
        trigger_type = trigger.get("type", "unknown")
    else:
        trigger_type = str(trigger)

    payload: dict[str, Any] = {
        "trigger": trigger_type,
        "trigger_payload": automation.trigger,
        "automation_id": str(automation.id),
        "automation_name": automation.name,
    }
    if run.event_payload:
        payload["event"] = run.event_payload
    if automation.model:
        payload["model"] = automation.model
    return payload


async def _execute_run(
    run: AutomationRun,
    settings: ServiceSettings,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
) -> None:
    """Execute a single run in a background task (fire-and-forget).

    Mode-agnostic execution flow:
    1. Build env vars and calculate timeout
    2. Get execution context (creates sandbox in Cloud, returns config in local)
    3. Prepare tarball source
    4. Execute in context (upload tarball, start entrypoint)
    5. Store sandbox_id for watchdog verification (if applicable)

    The SDK inside the execution environment fires the completion callback on exit.
    The watchdog will verify status if the callback is missed.
    """
    run_id = str(run.id)
    automation = run.automation
    automation_id = str(automation.id)
    tarball_path = automation.tarball_path
    backend = get_backend(run)

    def _log_ctx(sandbox_id: str | None = None) -> dict[str, Any]:
        return log_extra(
            run_id=run_id, automation_id=automation_id, sandbox_id=sandbox_id
        )

    async def _fail(error: str, disable: bool = False) -> None:
        """Mark run as failed and optionally disable the automation."""
        await mark_run_terminal(session_factory, run, AutomationRunStatus.FAILED, error)
        if disable:
            await disable_automation(session_factory, automation.id, error)

    # 1. Calculate effective timeout (doesn't depend on ctx)
    max_run_duration = get_config().sandbox.max_run_duration
    effective_timeout = (
        min(automation.timeout, max_run_duration)
        if automation.timeout
        else max_run_duration
    )

    # 2. Get execution context - if this fails, nothing to clean up
    # Note: This also initializes backend state (e.g., API key for cloud mode)
    try:
        ctx = await backend.get_execution_context(client)
    except ConcurrencyLimitReachedError as exc:
        logger.warning(
            "Run skipped — organization concurrency limit reached: %s",
            exc,
            extra=_log_ctx(),
        )
        await mark_run_terminal(session_factory, run, AutomationRunStatus.SKIPPED)
        return
    except Exception:
        logger.exception("Failed to get execution context", extra=_log_ctx())
        await _fail("Failed to get execution context")
        return

    logger.info(
        "Execution context ready: %s",
        ctx.agent_url,
        extra=_log_ctx(sandbox_id=ctx.sandbox_id),
    )

    # 3. Build env vars (must be after get_execution_context for cloud mode API key)
    callback_url = f"{settings.resolved_base_url.rstrip('/')}/v1/runs/{run_id}/complete"
    env_vars = backend.build_env_vars()
    env_vars["AUTOMATION_CALLBACK_URL"] = callback_url
    env_vars["AUTOMATION_RUN_ID"] = run_id
    env_vars["AUTOMATION_API_URL"] = settings.resolved_base_url
    env_vars["AUTOMATION_EVENT_PAYLOAD"] = json.dumps(
        _build_event_payload(automation, run)
    )
    if automation.model:
        env_vars["AUTOMATION_MODEL"] = automation.model
    if ctx.sandbox_id:
        env_vars["SANDBOX_ID"] = ctx.sandbox_id
        env_vars["SESSION_API_KEY"] = ctx.session_key

    # 4. Prepare tarball source
    try:
        tarball_source: bytes | str
        if is_http_url(tarball_path):
            tarball_source = tarball_path
            logger.info(
                "HTTP URL tarball, will download in environment",
                extra=_log_ctx(sandbox_id=ctx.sandbox_id),
            )
        else:
            upload_id = parse_internal_upload_id(tarball_path)
            if upload_id is None:
                raise ValueError(f"Unsupported tarball_path: {tarball_path!r}")
            async with session_factory() as session:
                tarball_source = await _download_internal_tarball(upload_id, session)
            logger.info(
                "Internal tarball downloaded (%d bytes)",
                len(tarball_source),
                extra=_log_ctx(sandbox_id=ctx.sandbox_id),
            )
    except PermanentDispatchError as exc:
        logger.error(
            "Permanent dispatch error, disabling automation: %s",
            exc,
            exc_info=True,
            extra=_log_ctx(sandbox_id=ctx.sandbox_id),
        )
        await backend.release_context(client, ctx)
        await _fail(str(exc), disable=True)
        return
    except (APIKeyError, ValueError) as exc:
        logger.error(
            "Dispatch error: %s",
            exc,
            exc_info=True,
            extra=_log_ctx(sandbox_id=ctx.sandbox_id),
        )
        await backend.release_context(client, ctx)
        await _fail(str(exc))
        return

    # 5. Execute in context
    work_dir = backend.get_work_dir(run_id)
    try:
        result = await execute_in_context(
            client=client,
            agent_url=ctx.agent_url,
            session_key=ctx.session_key,
            entrypoint=automation.entrypoint,
            tarball_source=tarball_source,
            work_dir=work_dir,
            env_vars=env_vars,
            timeout=effective_timeout,
            run_id=run_id,
            sandbox_id=ctx.sandbox_id,
        )
    except PermanentDispatchError as exc:
        logger.error(
            "Permanent dispatch error, disabling automation: %s",
            exc,
            exc_info=True,
            extra=_log_ctx(sandbox_id=ctx.sandbox_id),
        )
        await backend.release_context(client, ctx)
        await _fail(str(exc), disable=True)
        return
    except Exception:
        logger.exception(
            "Background execution failed", extra=_log_ctx(sandbox_id=ctx.sandbox_id)
        )
        await backend.release_context(client, ctx)
        await _fail("Internal error")
        return

    # 6. Handle result
    if result.success:
        if ctx.sandbox_id:
            await update_sandbox_id(session_factory, run.id, ctx.sandbox_id)
        if result.bash_command_id:
            # Persist the BashCommand id so the verifier can filter
            # BashOutput events by exactly this command (avoids
            # cross-command contamination on a shared agent server).
            await update_bash_command_id(
                session_factory, run.id, result.bash_command_id
            )
        logger.info(
            "Automation dispatched successfully, waiting for callback",
            extra=_log_ctx(sandbox_id=ctx.sandbox_id),
        )
        return

    logger.warning(
        "Execution failed: %s", result.error, extra=_log_ctx(sandbox_id=ctx.sandbox_id)
    )
    await backend.release_context(client, ctx)
    await _fail(result.error or "Execution failed")


async def dispatch_pending_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: ServiceSettings,
    client: httpx.AsyncClient,
    batch_size: int | None = None,
    max_run_duration: timedelta | None = None,
) -> list[AutomationRun]:
    """Poll for pending runs, mark RUNNING, and launch sandboxes.

    Each run is dispatched as an ``asyncio.create_task`` so the
    dispatcher loop is not blocked by long-running automations.

    Args:
        session_factory: Database session factory
        settings: Service settings for API access
        client: HTTP client for API calls (shared across runs)
        batch_size: Number of pending runs to fetch per poll (from config if None)
        max_run_duration: Default max duration for runs without custom timeout
    """
    # Use config defaults if not provided
    if batch_size is None or max_run_duration is None:
        config = get_config()
        if batch_size is None:
            batch_size = config.service.dispatcher_batch_size
        if max_run_duration is None:
            max_run_duration = timedelta(seconds=config.sandbox.max_run_duration)

    async with session_factory() as session:
        pending_runs = await _poll_pending_runs(session, batch_size)

        dispatched_runs = []
        for run in pending_runs:
            run_id = str(run.id)
            automation_id = str(run.automation_id) if run.automation_id else None
            extra = log_extra(run_id=run_id, automation_id=automation_id)
            try:
                logger.info("Dispatching automation run", extra=extra)
                # Use automation's custom timeout if set, otherwise use default
                run_max_duration = (
                    timedelta(seconds=run.automation.timeout)
                    if run.automation and run.automation.timeout
                    else max_run_duration
                )
                await mark_run_status(
                    session,
                    run,
                    AutomationRunStatus.RUNNING,
                    max_duration=run_max_duration,
                )
                dispatched_runs.append(run)
            except Exception:
                logger.exception("Failed to dispatch run", extra=extra)

        await session.commit()

        for run in dispatched_runs:
            asyncio.create_task(
                _execute_run_safe(run, settings, session_factory, client),
                name=f"execute-run-{run.id}",
            )

        return dispatched_runs


async def _execute_run_safe(
    run: AutomationRun,
    settings: ServiceSettings,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
) -> None:
    """Wrapper around ``_execute_run`` that never lets exceptions escape.

    ``asyncio.create_task`` silently swallows exceptions from background
    tasks, so this wrapper ensures every failure is logged and the run is
    marked FAILED.
    """
    run_id = str(run.id)
    automation_id = str(run.automation_id) if run.automation_id else None
    extra = log_extra(run_id=run_id, automation_id=automation_id)
    try:
        await _execute_run(run, settings, session_factory, client)
    except Exception:
        logger.exception("Background execution failed", extra=extra)
        await mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, "Internal error"
        )


async def dispatcher_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: ServiceSettings,
    interval_seconds: int | None = None,
    shutdown_event: asyncio.Event | None = None,
    batch_size: int | None = None,
) -> None:
    """Main dispatcher loop — polls for pending runs and dispatches them.

    The HTTP client is created once and kept open for the lifetime of the loop,
    allowing connection reuse across all dispatched runs.
    """
    # Load config once at loop start - all iterations use these values
    config = get_config()
    if interval_seconds is None:
        interval_seconds = config.service.dispatcher_interval_seconds
    if batch_size is None:
        batch_size = config.service.dispatcher_batch_size
    max_run_duration = timedelta(seconds=config.sandbox.max_run_duration)
    http_timeout = config.http.http_long_timeout

    logger.info(
        "Dispatcher started, polling every %d seconds (batch_size=%d)",
        interval_seconds,
        batch_size,
    )

    async with httpx.AsyncClient(timeout=http_timeout) as client:
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                logger.info("Dispatcher received shutdown signal, exiting")
                break

            try:
                dispatched = await dispatch_pending_runs(
                    session_factory,
                    settings=settings,
                    client=client,
                    batch_size=batch_size,
                    max_run_duration=max_run_duration,
                )
                if dispatched:
                    logger.info("Dispatched %d run(s)", len(dispatched))
                else:
                    logger.debug("No pending runs to dispatch")
            except Exception:
                logger.error("Error dispatching pending runs", exc_info=True)

            if shutdown_event is not None:
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(), timeout=interval_seconds
                    )
                    logger.info("Dispatcher received shutdown signal, exiting")
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_seconds)
