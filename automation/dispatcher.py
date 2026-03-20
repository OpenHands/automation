"""Dispatcher for processing pending automation runs.

Polls the automation_runs table for PENDING jobs and dispatches them
to sandboxes via the SaaS API.  Uses FOR UPDATE SKIP LOCKED for
multi-worker safety.

Completion is handled asynchronously: the SDK running inside the sandbox
POSTs to ``/api/v1/automations/runs/{id}/complete`` when the entry-point
exits, so the dispatcher does **not** block waiting for results.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from automation.execution import run_automation
from automation.models import AutomationRun, AutomationRunStatus
from automation.utils.api_key import APIKeyError, get_api_key_for_automation_run
from automation.utils.run import mark_run_status


logger = logging.getLogger("automation.dispatcher")

DEFAULT_BATCH_SIZE = 10
POLL_INTERVAL_SECONDS = 30


@dataclass
class DispatchConfig:
    """Runtime configuration for the dispatcher."""

    saas_api_url: str
    automation_service_url: str


async def _download_tarball(tarball_path: str) -> bytes:
    """Download the tarball from ``tarball_path``.

    Supports ``http://`` and ``https://`` URLs.  Cloud storage schemes
    (``gs://``, ``s3://``) will be added in a future iteration.
    """
    if tarball_path.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(tarball_path)
            resp.raise_for_status()
            return resp.content

    raise ValueError(
        f"Unsupported tarball_path scheme: {tarball_path!r}. "
        "Only http:// and https:// are supported for now."
    )


async def _poll_pending_runs(
    session: AsyncSession,
    batch_size: int,
) -> list[AutomationRun]:
    """Poll pending runs using FOR UPDATE SKIP LOCKED.

    Eagerly loads the ``automation`` relationship so that ``user_id``
    and ``org_id`` are available for API-key minting.
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
    config: DispatchConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Execute a single run in a background task.

    1. Mint a per-user API key via the service endpoint.
    2. Download the tarball from ``automation.tarball_path``.
    3. Call ``run_automation()`` to spin up a sandbox, upload, and execute.
    4. If the sandbox itself fails to start, mark the run FAILED.

    The SDK inside the sandbox fires the completion callback on exit,
    so we don't need to inspect the result for the happy path.
    """
    run_id = str(run.id)
    automation = run.automation
    callback_url = (
        f"{config.automation_service_url.rstrip('/')}"
        f"/api/v1/automations/runs/{run_id}/complete"
    )

    try:
        # 1. Get a per-user API key
        api_key = await get_api_key_for_automation_run(run)

        # 2. Download the tarball
        tarball = await _download_tarball(automation.tarball_path)

        # 3. Launch the sandbox
        result = await run_automation(
            api_url=config.saas_api_url,
            api_key=api_key,
            tarball=tarball,
            entrypoint=automation.entrypoint,
            callback_url=callback_url,
            run_id=run_id,
        )

        if not result.success:
            logger.warning("Run %s sandbox execution failed: %s", run_id, result.error)
            await _mark_run_failed(session_factory, run, result.error)

    except (APIKeyError, ValueError, httpx.HTTPError) as exc:
        logger.error("Run %s dispatch error: %s", run_id, exc)
        await _mark_run_failed(session_factory, run, str(exc))
    except Exception:
        logger.exception("Background execution failed for run %s", run_id)
        await _mark_run_failed(session_factory, run, "Internal dispatcher error")


async def _mark_run_failed(
    session_factory: async_sessionmaker[AsyncSession],
    run: AutomationRun,
    error: str | None,
) -> None:
    """Mark a run as FAILED if it's still RUNNING."""
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
                    AutomationRunStatus.FAILED,
                    error_detail=error,
                )
                await session.commit()
    except Exception:
        logger.exception("Failed to mark run %s as FAILED", run.id)


async def dispatch_pending_runs(
    session_factory: async_sessionmaker[AsyncSession],
    config: DispatchConfig | None = None,
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
            try:
                logger.info("Dispatching automation run %s", run.id)
                await mark_run_status(session, run, AutomationRunStatus.RUNNING)
                dispatched_runs.append(run)
            except Exception:
                logger.exception("Failed to dispatch run %s", run.id)

        await session.commit()

        if config:
            for run in dispatched_runs:
                asyncio.create_task(_execute_run(run, config, session_factory))

        return dispatched_runs


async def dispatcher_loop(
    session_factory: async_sessionmaker[AsyncSession],
    config: DispatchConfig | None = None,
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
                session_factory, config=config, batch_size=batch_size
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
