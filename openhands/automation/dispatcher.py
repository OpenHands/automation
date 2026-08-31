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
from openhands.automation.subjects import conversation_id_for
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.utils import log_extra
from openhands.automation.utils.api_key import APIKeyError
from openhands.automation.utils.kv import create_kv_token
from openhands.automation.utils.run import (
    disable_automation,
    mark_run_status,
    mark_run_terminal,
    update_bash_command_id,
    update_run_current_phase,
    update_run_timeout_at,
    update_sandbox_id,
)
from openhands.automation.utils.run_status_detail import (
    RunStatusDetailKind,
    RunStatusPhase,
    make_run_status_detail,
    run_status_detail_from_exception,
)
from openhands.automation.utils.tarball_validation import (
    is_http_url,
    parse_internal_upload_id,
)
from openhands.automation.utils.time import utcnow
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds
from openhands.automation.utils.unhealthy import (
    maybe_disable_unhealthy_automation_after_run,
)


logger = logging.getLogger("automation.dispatcher")


async def _download_internal_tarball(
    upload_id: uuid.UUID,
    session: AsyncSession | None,
) -> bytes:
    """Download a tarball from storage using the TarballUpload record.

    Raises:
        TarballNotFoundError: If the tarball upload record doesn't exist, or if
            the record exists but its storage object is missing. Both are
            permanent errors that should disable the automation.
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

    from openhands.automation.storage import ObjectNotFoundError, get_file_store

    store = get_file_store()
    try:
        return store.read(upload.storage_path)
    except ObjectNotFoundError as e:
        # Only confirmed absence is permanent; transient storage errors raise
        # plain FileNotFoundError and keep retrying on the next schedule tick.
        raise TarballNotFoundError(
            f"Internal tarball object missing from storage at "
            f"{upload.storage_path!r} for upload {upload_id}. "
            "Recreate the automation to restore it."
        ) from e


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
        .join(AutomationRun.automation)
        .options(selectinload(AutomationRun.automation))
        .where(
            AutomationRun.status == AutomationRunStatus.PENDING,
            Automation.enabled.is_(True),
            Automation.deleted_at.is_(None),
        )
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

    async def _fail(
        error: str,
        disable: bool = False,
        status_detail: dict | None = None,
    ) -> None:
        """Mark run as failed and optionally disable the automation."""
        await mark_run_terminal(
            session_factory,
            run,
            AutomationRunStatus.FAILED,
            error,
            status_detail=status_detail,
        )
        automation_disabled = disable
        if disable:
            automation_disabled = await disable_automation(
                session_factory,
                automation.id,
                error,
                disabled_detail={"status_detail": status_detail}
                if status_detail is not None
                else None,
                run_id=run.id,
            )
        elif status_detail is not None:
            automation_disabled = await maybe_disable_unhealthy_automation_after_run(
                session_factory,
                automation.id,
            )
        await capture_automation_event(
            "automation_run_failed",
            automation=automation,
            run=run,
            session_factory=session_factory,
            properties={
                "trigger_source": "dispatcher",
                "failure_kind": "dispatch_error",
                "automation_disabled": automation_disabled,
            },
        )

    # 1. Calculate effective timeout (doesn't depend on ctx). This same value
    # drives both the bash command timeout and the watchdog cleanup deadline.
    effective_timeout = resolve_automation_timeout_seconds(automation.timeout)

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
        status_detail = make_run_status_detail(
            phase=RunStatusPhase.DISPATCH,
            kind=RunStatusDetailKind.CONCURRENCY_LIMIT,
            detail=str(exc),
            transient=True,
            source="sandbox_api",
            operation="get_execution_context",
        )
        await mark_run_terminal(
            session_factory,
            run,
            AutomationRunStatus.SKIPPED,
            status_detail=status_detail,
        )
        await capture_automation_event(
            "automation_run_skipped",
            automation=automation,
            run=run,
            session_factory=session_factory,
            properties={
                "trigger_source": "dispatcher",
                "skip_reason": "concurrency_limit",
            },
        )
        return
    except Exception as exc:
        logger.exception("Failed to get execution context", extra=_log_ctx())
        source = "agent_server" if backend.is_local_mode else "sandbox_api"
        await _fail(
            "Failed to get execution context",
            status_detail=run_status_detail_from_exception(
                exc,
                phase=RunStatusPhase.DISPATCH,
                source=source,
                operation="get_execution_context",
            ),
        )
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
    env_vars["AUTOMATION_PHASE_URL"] = (
        f"{settings.resolved_base_url.rstrip('/')}/v1/runs/{run_id}/phase"
    )
    env_vars["AUTOMATION_RUN_ID"] = run_id
    env_vars["AUTOMATION_USER_ID"] = str(automation.user_id)
    env_vars["AUTOMATION_ORG_ID"] = str(automation.org_id)
    env_vars["AUTOMATION_API_URL"] = settings.resolved_base_url
    env_vars["AUTOMATION_EVENT_PAYLOAD"] = json.dumps(
        _build_event_payload(automation, run)
    )
    # A subject-owning run has to create its conversation under the very id
    # `continue_conversation` will address later. Derive it here rather than
    # letting the script mint a random one: otherwise every follow-up event
    # POSTs to an id that does not exist, `send_conversation_turn` swallows
    # the 404 as an ordinary reaped sandbox, and the thread silently starts a
    # fresh conversation on every turn.
    if run.subject_key:
        trigger_source = (automation.trigger or {}).get("source")
        if trigger_source:
            env_vars["AUTOMATION_CONVERSATION_ID"] = conversation_id_for(
                automation.org_id,
                automation.id,
                trigger_source,
                run.subject_key,
            )
    if automation.model:
        env_vars["AUTOMATION_MODEL"] = automation.model
    if ctx.sandbox_id:
        env_vars["SANDBOX_ID"] = ctx.sandbox_id
        env_vars["SESSION_API_KEY"] = ctx.session_key

    # Inject a KV token whenever the service has a KV secret configured.
    # The KV store is always available to automations — there is no per-
    # automation toggle. If no secret is configured the feature is simply
    # disabled service-wide.
    kv_config = get_config().kv
    if kv_config.kv_secret:
        env_vars["AUTOMATION_KV_TOKEN"] = create_kv_token(
            secret=kv_config.kv_secret,
            automation_id=automation.id,
            run_id=run.id,
        )

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
        await _fail(
            str(exc),
            disable=True,
            status_detail=make_run_status_detail(
                phase=RunStatusPhase.DISPATCH,
                kind=RunStatusDetailKind.UNKNOWN,
                detail=str(exc),
                transient=False,
                source="automation_service",
                operation="prepare_tarball",
                code=type(exc).__name__,
            ),
        )
        return
    except (APIKeyError, ValueError) as exc:
        logger.error(
            "Dispatch error: %s",
            exc,
            exc_info=True,
            extra=_log_ctx(sandbox_id=ctx.sandbox_id),
        )
        await backend.release_context(client, ctx)
        await _fail(
            str(exc),
            status_detail=make_run_status_detail(
                phase=RunStatusPhase.DISPATCH,
                kind=RunStatusDetailKind.UNKNOWN,
                detail=str(exc),
                transient=False,
                source="automation_service",
                operation="prepare_tarball",
                code=type(exc).__name__,
            ),
        )
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
        await _fail(
            str(exc),
            disable=True,
            status_detail=make_run_status_detail(
                phase=RunStatusPhase.EXECUTION,
                kind=RunStatusDetailKind.UNKNOWN,
                detail=str(exc),
                transient=False,
                source="automation_service",
                operation="execute_in_context",
                code=type(exc).__name__,
            ),
        )
        return
    except Exception as exc:
        logger.exception(
            "Background execution failed", extra=_log_ctx(sandbox_id=ctx.sandbox_id)
        )
        await backend.release_context(client, ctx)
        source = "agent_server" if backend.is_local_mode else "sandbox_api"
        await _fail(
            "Internal error",
            status_detail=run_status_detail_from_exception(
                exc,
                phase=RunStatusPhase.EXECUTION,
                source=source,
                operation="execute_in_context",
            ),
        )
        return

    # 6. Handle result
    if result.success:
        await update_run_current_phase(session_factory, run.id, "Starting automation")
        if ctx.sandbox_id:
            await update_sandbox_id(session_factory, run.id, ctx.sandbox_id)
        if result.bash_command_id:
            # Persist the BashCommand id so the verifier can filter
            # BashOutput events by exactly this command (avoids
            # cross-command contamination on a shared agent server).
            await update_bash_command_id(
                session_factory, run.id, result.bash_command_id
            )
        # Phase-2 deadline: the bash command has started, so its own timeout
        # (enforced by the agent-server from bash start) now governs the run.
        # Align the watchdog deadline with it, plus margin so the bash
        # service's kill always fires first and verification finds a
        # concrete exit code instead of a still-running command.
        await update_run_timeout_at(
            session_factory,
            run.id,
            utcnow()
            + timedelta(
                seconds=effective_timeout + get_config().sandbox.run_timeout_margin
            ),
        )
        logger.info(
            "Automation dispatched successfully, waiting for callback",
            extra=_log_ctx(sandbox_id=ctx.sandbox_id),
        )
        return

    error = result.error or "Execution failed"
    logger.warning(
        "Execution failed: %s", result.error, extra=_log_ctx(sandbox_id=ctx.sandbox_id)
    )
    await backend.release_context(client, ctx)
    await _fail(
        error,
        status_detail=make_run_status_detail(
            phase=RunStatusPhase.EXECUTION,
            kind=RunStatusDetailKind.EXECUTION_ERROR,
            detail=error,
            transient=False,
            source="agent_server" if backend.is_local_mode else "sandbox_api",
            operation="execute_in_context",
        ),
    )


async def dispatch_pending_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: ServiceSettings,
    client: httpx.AsyncClient,
    batch_size: int | None = None,
) -> list[AutomationRun]:
    """Poll for pending runs, mark RUNNING, and launch sandboxes.

    Each run is dispatched as an ``asyncio.create_task`` so the
    dispatcher loop is not blocked by long-running automations.

    Args:
        session_factory: Database session factory
        settings: Service settings for API access
        client: HTTP client for API calls (shared across runs)
        batch_size: Number of pending runs to fetch per poll (from config if None)
    """
    # Use config defaults if not provided
    if batch_size is None:
        config = get_config()
        batch_size = config.service.dispatcher_batch_size

    async with session_factory() as session:
        pending_runs = await _poll_pending_runs(session, batch_size)

        dispatched_runs = []
        for run in pending_runs:
            run_id = str(run.id)
            automation_id = str(run.automation_id) if run.automation_id else None
            extra = log_extra(run_id=run_id, automation_id=automation_id)
            try:
                logger.info("Dispatching automation run", extra=extra)
                run_timeout_seconds = resolve_automation_timeout_seconds(
                    run.automation.timeout if run.automation else None
                )
                # Phase-1 provisioning deadline: pads the run budget with the
                # sandbox-ready budget and margin so the watchdog only reaps
                # runs that die during provisioning. Once the bash command
                # actually starts, _execute_run resets timeout_at to
                # bash-start + run budget + margin (phase 2).
                sandbox_cfg = get_config().sandbox
                provisioning_deadline = (
                    sandbox_cfg.sandbox_ready_timeout
                    + run_timeout_seconds
                    + sandbox_cfg.run_timeout_margin
                )
                await mark_run_status(
                    session,
                    run,
                    AutomationRunStatus.RUNNING,
                    max_duration=timedelta(seconds=provisioning_deadline),
                    current_phase="Preparing environment",
                )
                dispatched_runs.append(run)
            except Exception:
                logger.exception("Failed to dispatch run", extra=extra)
                await capture_automation_event(
                    "automation_run_dispatch_failed",
                    automation=run.automation,
                    run=run,
                    properties={"trigger_source": "dispatcher"},
                    session=session,
                )

        await session.commit()

        for run in dispatched_runs:
            await capture_automation_event(
                "automation_run_dispatched",
                automation=run.automation,
                run=run,
                properties={"trigger_source": "dispatcher"},
                session_factory=session_factory,
            )
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
    except Exception as exc:
        logger.exception("Background execution failed", extra=extra)
        await mark_run_terminal(
            session_factory,
            run,
            AutomationRunStatus.FAILED,
            "Internal error",
            status_detail=run_status_detail_from_exception(
                exc,
                phase=RunStatusPhase.DISPATCH,
                source="automation_service",
                operation="execute_run_task",
            ),
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
