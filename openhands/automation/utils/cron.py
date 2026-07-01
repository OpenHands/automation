"""Cron schedule utilities.

Pure functions for computing cron fire times and determining if automations
are due to execute. These functions handle timezone conversion and croniter
interactions.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import (
    CroniterBadDateError,
    CroniterBadTypeRangeError,
    CroniterError,
    croniter,
)

from openhands.automation.utils.time import utcnow


if TYPE_CHECKING:
    from openhands.automation.models import Automation


_CRON_VALIDATION_BASE_TIME = datetime(2026, 7, 1)


def validate_timezone_name(timezone: str) -> str:
    """Validate and return an IANA timezone name."""
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"Invalid timezone: {timezone}") from e
    return timezone


def validate_cron_schedule(cron_schedule: str) -> str:
    """Validate that a cron expression is syntactically valid and can fire.

    ``croniter.is_valid`` accepts impossible dates like ``0 0 31 2 *``.
    Verify that croniter can compute at least one future fire time so these
    schedules are rejected before they can poison scheduler polling.
    """
    if not croniter.is_valid(cron_schedule):
        raise ValueError(f"Invalid cron expression: {cron_schedule}")

    try:
        croniter(cron_schedule, _CRON_VALIDATION_BASE_TIME).get_next(datetime)
    except CroniterBadDateError as e:
        raise ValueError(
            f"Cron expression cannot produce any future fire times: {cron_schedule}"
        ) from e
    except (CroniterError, CroniterBadTypeRangeError) as e:
        raise ValueError(f"Invalid cron expression: {cron_schedule}") from e

    return cron_schedule


def get_next_fire_time(
    cron_schedule: str,
    timezone: str = "UTC",
    base_time: datetime | None = None,
) -> datetime:
    """Calculate the next fire time for a cron schedule.

    Args:
        cron_schedule: Cron expression (e.g., '0 9 * * 5')
        timezone: IANA timezone name (e.g., 'America/New_York')
        base_time: Base time for calculation (defaults to now, UTC-aware)

    Returns:
        Next fire time as a UTC-aware datetime
    """
    if base_time is None:
        base_time = utcnow()

    tz = ZoneInfo(timezone)

    # Ensure base_time is aware (treat naive as UTC for safety)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=ZoneInfo("UTC"))

    # Convert to the target timezone, then strip tzinfo for croniter
    base_in_tz_naive = base_time.astimezone(tz).replace(tzinfo=None)

    # croniter computes the next fire time in the target timezone
    cron = croniter(cron_schedule, base_in_tz_naive)
    next_fire_in_tz = cron.get_next(datetime)

    # Convert back to UTC-aware
    return next_fire_in_tz.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))


def get_prev_fire_time(
    cron_schedule: str,
    timezone: str = "UTC",
    base_time: datetime | None = None,
) -> datetime:
    """Calculate the previous (most recent) fire time for a cron schedule.

    Args:
        cron_schedule: Cron expression (e.g., '0 9 * * 5')
        timezone: IANA timezone name (e.g., 'America/New_York')
        base_time: Base time for calculation (defaults to now, UTC-aware)

    Returns:
        Previous fire time as a UTC-aware datetime
    """
    if base_time is None:
        base_time = utcnow()

    tz = ZoneInfo(timezone)

    # Ensure base_time is aware (treat naive as UTC for safety)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=ZoneInfo("UTC"))

    # Convert to the target timezone, then strip tzinfo for croniter
    base_in_tz_naive = base_time.astimezone(tz).replace(tzinfo=None)

    # croniter computes the previous fire time in the target timezone
    cron = croniter(cron_schedule, base_in_tz_naive)
    prev_fire_in_tz = cron.get_prev(datetime)

    # Convert back to UTC-aware
    return prev_fire_in_tz.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))


def is_automation_due(
    automation: Automation,
    now: datetime | None = None,
) -> bool:
    """Check if an automation is due to fire.

    An automation is due if:
    1. It's enabled and not deleted
    2. Its next fire time (based on cron schedule) is <= now
    3. It hasn't been triggered since its last due time

    Args:
        automation: The automation to check
        now: Current time (defaults to now, naive UTC)

    Returns:
        True if the automation should fire
    """
    if now is None:
        now = utcnow()

    if not automation.enabled or automation.deleted_at is not None:
        return False

    trigger = automation.trigger
    if trigger.get("type") != "cron":
        return False

    schedule = trigger.get("schedule")
    if not schedule:
        return False

    timezone = trigger.get("timezone", "UTC")

    # Calculate the previous fire time (most recent time the cron should have fired)
    # in the user's configured timezone, converted back to UTC
    prev_fire_time = get_prev_fire_time(schedule, timezone, now)

    # Determine the reference time (last trigger, or creation time if never triggered)
    if automation.last_triggered_at is None:
        # Never triggered - use created_at as reference (no catch-up on old schedules)
        reference_time = automation.created_at
    else:
        reference_time = automation.last_triggered_at

    # Ensure reference_time is aware (treat naive as UTC for safety)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=ZoneInfo("UTC"))

    # Due if a scheduled fire time has passed since the reference time
    return prev_fire_time > reference_time
