"""Dispatcher for processing pending automation runs.

Polls the automation_runs table for PENDING jobs and dispatches them
to sandboxes (Cloud mode) or a local agent server (local mode).

Uses FOR UPDATE SKIP LOCKED for multi-worker safety (PostgreSQL).
SQLite deployments skip row locking (single-process mode assumed).

Completion is handled asynchronously: the SDK running inside the sandbox
POSTs to ``/v1/runs/{id}/complete`` when the entry-point
exits, so the dispatcher does **not** block waiting for results.

Local mode vs Cloud mode:
- Cloud mode: Creates sandbox per run, mints per-user API key
- Local mode: Uses pre-configured agent server, uses config-level API key
"""

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from automation.config import ServiceSettings, get_config
from automation.db import using_sqlite
from automation.exceptions import PermanentDispatchError, TarballNotFoundError
from automation.execution import dispatch_automation
from automation.models import AutomationRun, AutomationRunStatus, TarballUpload
from automation.utils import log_extra
from automation.utils.api_key import APIKeyError, get_api_key_for_automation_run
from automation.utils.run import (
    disable_automation,
    mark_run_status,
    mark_run_terminal,
    update_sandbox_id,
)
from automation.utils.tarball_validation import is_http_url, parse_internal_upload_id


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

    from automation.storage import get_file_store

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


async def _execute_run(
    run: AutomationRun,
    settings: ServiceSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Execute a single run in a background task (fire-and-forget).

    Cloud mode:
    1. Fetch a per-user API key from the SaaS service (on demand, never stored).
    2. Determine tarball source and dispatch to a new sandbox.
    3. Store sandbox_id for watchdog verification.

    Local mode:
    1. Use config-level API key (no per-user key minting).
    2. Dispatch to the pre-configured local agent server.
    3. No sandbox_id to store (persistent server).

    The SDK inside the sandbox fires the completion callback on exit.
    The watchdog will verify status if the callback is missed.
    """
    run_id = str(run.id)
    automation = run.automation
    automation_id = str(automation.id)
    tarball_path = automation.tarball_path
    is_local = settings.is_local_mode

    # Helper for consistent structured logging with run context
    def _log_ctx(sandbox_id: str | None = None) -> dict[str, Any]:
        return log_extra(
            run_id=run_id, automation_id=automation_id, sandbox_id=sandbox_id
        )

    callback_url = f"{settings.resolved_base_url.rstrip('/')}/v1/runs/{run_id}/complete"

    try:
        # 1. Get API key: local mode uses config key, cloud mode mints per-user key
        if is_local:
            api_key = settings.agent_server_api_key
            api_url = settings.agent_server_url
            logger.info("Local mode: using configured agent server", extra=_log_ctx())
        else:
            api_key = await get_api_key_for_automation_run(run)
            api_url = settings.openhands_api_base_url

        # 2. Determine tarball source
        tarball_source: bytes | str
        if is_http_url(tarball_path):
            # HTTP(S) URL: download directly inside sandbox (untrusted/large)
            tarball_source = tarball_path
            logger.info("HTTP URL tarball, will download in sandbox", extra=_log_ctx())
        else:
            # Internal (oh-internal://): download from storage, upload to sandbox
            upload_id = parse_internal_upload_id(tarball_path)
            if upload_id is None:
                raise ValueError(f"Unsupported tarball_path: {tarball_path!r}")

            async with session_factory() as session:
                tarball_source = await _download_internal_tarball(upload_id, session)
            logger.info(
                "Internal tarball downloaded (%d bytes)",
                len(tarball_source),
                extra=_log_ctx(),
            )

        # 3. Build env vars for the execution environment
        env_vars: dict[str, str] = {}

        if is_local:
            # Local mode: inject agent server URL for SDK's RemoteWorkspace
            env_vars["AGENT_SERVER_URL"] = settings.agent_server_url
            # If there's an OpenHands API key in config, inject it for LLM/secrets
            if settings.openhands_api_base_url:
                env_vars["OPENHANDS_CLOUD_API_URL"] = settings.openhands_api_base_url
        else:
            # Cloud mode: inject Cloud API credentials
            env_vars["OPENHANDS_API_KEY"] = api_key
            env_vars["OPENHANDS_CLOUD_API_URL"] = settings.openhands_api_base_url

        # Trigger context so the SDK script knows *why* it was invoked
        # Includes automation metadata and event payload (for event-triggered runs)
        trigger_context = {
            "trigger": automation.trigger,
            "automation_id": str(automation.id),
            "automation_name": automation.name,
        }

        # Include webhook event payload if this is an event-triggered run
        if run.event_payload is not None:
            trigger_context["event"] = run.event_payload

        env_vars["AUTOMATION_EVENT_PAYLOAD"] = json.dumps(trigger_context)

        # 4. Calculate effective timeout: use automation's timeout if set,
        # capped at system maximum; otherwise use system default
        max_run_duration = get_config().sandbox.max_run_duration
        if automation.timeout is not None:
            effective_timeout = min(automation.timeout, max_run_duration)
        else:
            effective_timeout = max_run_duration

        # 5. Dispatch to sandbox/agent server (fire-and-forget)
        result = await dispatch_automation(
            api_url=api_url,
            api_key=api_key,
            entrypoint=automation.entrypoint,
            tarball_source=tarball_source,
            env_vars=env_vars,
            timeout=effective_timeout,
            callback_url=callback_url,
            run_id=run_id,
        )

        sandbox_extra = _log_ctx(sandbox_id=result.sandbox_id)
        if result.success:
            # Store sandbox_id for later verification by the watchdog (Cloud mode only)
            if result.sandbox_id:
                await update_sandbox_id(session_factory, run.id, result.sandbox_id)
            logger.info(
                "Automation dispatched successfully, waiting for callback",
                extra=sandbox_extra,
            )
            # Don't mark as COMPLETED here - wait for the callback
        else:
            logger.warning(
                "Dispatch failed: %s",
                result.error,
                extra=sandbox_extra,
            )
            await mark_run_terminal(
                session_factory, run, AutomationRunStatus.FAILED, result.error
            )

    except PermanentDispatchError as exc:
        # Permanent configuration error - disable the automation
        logger.error(
            "Permanent dispatch error, disabling automation: %s",
            exc,
            exc_info=True,
            extra=_log_ctx(),
        )
        await mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, str(exc)
        )
        await disable_automation(session_factory, automation.id, str(exc))

    except (APIKeyError, ValueError) as exc:
        logger.error("Dispatch error: %s", exc, exc_info=True, extra=_log_ctx())
        await mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, str(exc)
        )
    except Exception:
        logger.exception("Background execution failed", extra=_log_ctx())
        await mark_run_terminal(
            session_factory, run, AutomationRunStatus.FAILED, "Internal error"
        )


async def dispatch_pending_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: ServiceSettings,
    batch_size: int | None = None,
    max_run_duration: timedelta | None = None,
) -> list[AutomationRun]:
    """Poll for pending runs, mark RUNNING, and launch sandboxes.

    Each run is dispatched as an ``asyncio.create_task`` so the
    dispatcher loop is not blocked by long-running automations.

    Args:
        session_factory: Database session factory
        settings: Service settings for API access
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
                _execute_run_safe(run, settings, session_factory),
                name=f"execute-run-{run.id}",
            )

        return dispatched_runs


async def _execute_run_safe(
    run: AutomationRun,
    settings: ServiceSettings,
    session_factory: async_sessionmaker[AsyncSession],
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
        await _execute_run(run, settings, session_factory)
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
    """Main dispatcher loop — polls for pending runs and dispatches them."""
    # Load config once at loop start - all iterations use these values
    config = get_config()
    if interval_seconds is None:
        interval_seconds = config.service.dispatcher_interval_seconds
    if batch_size is None:
        batch_size = config.service.dispatcher_batch_size
    max_run_duration = timedelta(seconds=config.sandbox.max_run_duration)

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
                session_factory,
                settings=settings,
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
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
                logger.info("Dispatcher received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
