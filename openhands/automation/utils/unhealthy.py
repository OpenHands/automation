"""Classify unhealthy automations and auto-disable chronic permanent failures."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.config import get_config
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.models import (
    Automation,
    AutomationDisableEvent,
    AutomationRun,
    AutomationRunStatus,
)
from openhands.automation.utils.run import skip_pending_runs_for_disabled_automation
from openhands.automation.utils.time import utcnow


logger = logging.getLogger(__name__)

PERMANENT_FAILURE_KINDS = frozenset({"auth", "config", "quota", "blocked"})
TERMINAL_OUTCOME_STATUSES = (
    AutomationRunStatus.COMPLETED,
    AutomationRunStatus.FAILED,
)


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def is_permanent_failure_detail(detail: Mapping[str, Any] | None) -> bool:
    """Return whether status_detail describes a non-transient config fault."""
    if not detail:
        return False
    if detail.get("transient") is True:
        return False
    if detail.get("permanent") is True:
        return True

    kind = _string_value(detail.get("kind"))
    if kind and kind.casefold() in PERMANENT_FAILURE_KINDS:
        return True

    classification = detail.get("classification")
    if isinstance(classification, Mapping):
        if classification.get("retryable") is True:
            return False
        classification_kind = _string_value(classification.get("kind"))
        if (
            classification_kind
            and classification_kind.casefold() in PERMANENT_FAILURE_KINDS
        ):
            return True
        if classification.get("user_action") == "settings":
            return True

    return detail.get("user_action") == "settings"


def _disabled_reason(detail: Mapping[str, Any], count: int) -> str:
    formatted = _string_value(detail.get("formatted_detail"))
    message = formatted or _string_value(detail.get("detail")) or "Permanent failure"
    kind = _string_value(detail.get("kind")) or "permanent_failure"
    return f"{kind}: {message} (seen in {count} consecutive runs)"


async def get_consecutive_permanent_failure_count(
    session: AsyncSession,
    automation_id: uuid.UUID,
    *,
    limit: int,
) -> tuple[int, dict[str, Any] | None, uuid.UUID | None]:
    """Count latest consecutive terminal runs with permanent failure details."""
    result = await session.execute(
        select(AutomationRun)
        .where(
            AutomationRun.automation_id == automation_id,
            AutomationRun.status.in_(TERMINAL_OUTCOME_STATUSES),
        )
        .order_by(AutomationRun.created_at.desc())
        .limit(limit)
    )

    count = 0
    latest_detail: dict[str, Any] | None = None
    latest_run_id: uuid.UUID | None = None
    for run in result.scalars().all():
        detail = run.status_detail
        if not is_permanent_failure_detail(detail):
            break
        count += 1
        if latest_detail is None:
            latest_detail = detail
            latest_run_id = run.id
    return count, latest_detail, latest_run_id


async def maybe_disable_unhealthy_automation(
    session: AsyncSession,
    automation_id: uuid.UUID,
    *,
    threshold: int | None = None,
) -> bool:
    """Disable an automation once permanent failures reach the threshold."""
    if threshold is None:
        threshold = get_config().service.failure_disable_threshold
    if threshold <= 0:
        return False

    count, latest_detail, latest_run_id = await get_consecutive_permanent_failure_count(
        session,
        automation_id,
        limit=threshold,
    )
    if count < threshold or latest_detail is None:
        return False

    disabled_reason = _disabled_reason(latest_detail, count)
    disabled_detail = {
        "reason": disabled_reason,
        "threshold": threshold,
        "consecutive_permanent_failures": count,
        "run_id": str(latest_run_id) if latest_run_id else None,
        "status_detail": latest_detail,
    }
    disabled_at = utcnow()
    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(Automation)
        .where(
            Automation.id == automation_id,
            Automation.enabled == True,  # noqa: E712
        )
        .values(
            enabled=False,
            disabled_reason=disabled_reason,
            disabled_detail=disabled_detail,
            disabled_at=disabled_at,
        )
    )
    if result.rowcount == 0:
        return False

    await skip_pending_runs_for_disabled_automation(
        session,
        automation_id,
        reason=disabled_reason,
        disabled_detail=disabled_detail,
        completed_at=disabled_at,
    )

    automation = await session.get(Automation, automation_id)
    if automation is not None:
        await mark_git_sync_dirty(session, automation)

    session.add(
        AutomationDisableEvent(
            automation_id=automation_id,
            run_id=latest_run_id,
            reason=disabled_reason,
            detail=disabled_detail,
            source="consecutive_permanent_failures",
        )
    )

    logger.warning(
        "Automation disabled after %s consecutive permanent failures",
        count,
        extra={"automation_id": str(automation_id), "run_id": str(latest_run_id)},
    )
    return True


async def maybe_disable_unhealthy_automation_after_run(
    session_factory: async_sessionmaker[AsyncSession],
    automation_id: uuid.UUID,
    *,
    threshold: int | None = None,
) -> bool:
    """Open a short transaction and maybe auto-disable an automation."""
    try:
        async with session_factory() as session:
            disabled = await maybe_disable_unhealthy_automation(
                session,
                automation_id,
                threshold=threshold,
            )
            if disabled:
                await session.commit()
            return disabled
    except Exception:
        logger.exception(
            "Failed to evaluate automation unhealthy state",
            extra={"automation_id": str(automation_id)},
        )
        return False
