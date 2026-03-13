"""Cron scheduler: evaluates cron expressions and fires due automations.

Runs as a background asyncio task inside the FastAPI process.
Every SCHEDULER_INTERVAL_SECONDS, it:
1. Queries all enabled automations with cron triggers
2. Evaluates which are due (cron expression + last_triggered_at)
3. Creates a PENDING run for each due automation
4. Executes each run by calling the V1 API
"""

import asyncio
import logging
from datetime import UTC, datetime

from croniter import croniter
from sqlalchemy import select, update

from automation.config import get_settings
from automation.db import get_session_factory
from automation.encryption import decrypt_api_key
from automation.executor import execute_automation
from automation.models import Automation, AutomationRun, AutomationRunStatus


logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None


def is_cron_due(
    cron_expr: str,
    last_triggered: datetime | None,
    now: datetime,
    tolerance_seconds: int = 120,
) -> bool:
    """Check if a cron expression is due for firing.

    Returns True if the cron's most recent fire time falls between
    last_triggered and now (with a tolerance window to avoid missing fires
    due to scheduler drift).
    """
    if last_triggered is None:
        # Never triggered — check if the previous fire time is within tolerance
        cron = croniter(cron_expr, now)
        prev_fire = cron.get_prev(datetime)
        return (now - prev_fire).total_seconds() <= tolerance_seconds

    cron = croniter(cron_expr, last_triggered)
    next_fire = cron.get_next(datetime)
    return next_fire <= now


async def _run_scheduler_tick() -> None:
    """Single tick of the scheduler: find and execute due automations."""
    now = datetime.now(UTC)
    factory = await get_session_factory()

    async with factory() as session:
        # Fetch all enabled automations
        result = await session.execute(
            select(Automation).where(Automation.enabled.is_(True))
        )
        automations = result.scalars().all()

    for auto in automations:
        try:
            await _process_automation(auto, now)
        except Exception:
            logger.exception("Error processing automation %s", auto.id)


async def _process_automation(auto: Automation, now: datetime) -> None:
    """Check if an automation is due and fire it."""
    triggers = auto.triggers or {}
    cron_config = triggers.get("cron")
    if not cron_config:
        return

    schedule = cron_config.get("schedule")
    if not schedule:
        return

    if not is_cron_due(schedule, auto.last_triggered_at, now):
        return

    logger.info("Automation %s (%s) is due — firing", auto.id, auto.name)

    factory = await get_session_factory()
    async with factory() as session:
        # Create a run record
        run = AutomationRun(
            automation_id=auto.id,
            status=AutomationRunStatus.RUNNING,
            trigger_type="cron",
            started_at=now,
        )
        session.add(run)

        # Update last_triggered_at
        await session.execute(
            update(Automation)
            .where(Automation.id == auto.id)
            .values(last_triggered_at=now)
        )
        await session.commit()

        # Decrypt the API key and execute
        try:
            api_key = decrypt_api_key(auto.encrypted_api_key)
        except ValueError:
            run.status = AutomationRunStatus.FAILED
            run.error_detail = "Failed to decrypt stored API key"
            run.completed_at = datetime.now(UTC)
            await session.commit()
            logger.error("Cannot decrypt API key for automation %s", auto.id)
            return

        result = await execute_automation(
            api_key=api_key,
            sdk_code_tarball_path=auto.sdk_code_tarball_path,
        )

        if result.success:
            run.status = AutomationRunStatus.COMPLETED
            run.conversation_id = result.conversation_id
        else:
            run.status = AutomationRunStatus.FAILED
            run.error_detail = result.error

        run.completed_at = datetime.now(UTC)
        await session.commit()

        logger.info(
            "Automation %s run %s finished: %s",
            auto.id,
            run.id,
            run.status,
        )


async def scheduler_loop() -> None:
    """Main scheduler loop — runs forever, ticking at the configured interval."""
    settings = get_settings()
    interval = settings.scheduler_interval_seconds
    logger.info("Scheduler started (interval=%ds)", interval)

    while True:
        try:
            await _run_scheduler_tick()
        except Exception:
            logger.exception("Scheduler tick failed")

        await asyncio.sleep(interval)


def start_scheduler() -> asyncio.Task:
    """Start the scheduler as a background task."""
    global _scheduler_task
    _scheduler_task = asyncio.create_task(scheduler_loop())
    return _scheduler_task


def stop_scheduler() -> None:
    """Cancel the scheduler background task."""
    global _scheduler_task
    if _scheduler_task is not None:
        _scheduler_task.cancel()
        _scheduler_task = None
