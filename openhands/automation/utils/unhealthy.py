"""Auto-disable for automations that keep failing.

Two rules, both evaluated from a single fetch of the automation's most recent
terminal runs:

1. Consecutive *permanent* failures (bad key, revoked token). Unambiguous, so
   it fires fast and needs no time guard.
2. The last N runs all failed and nothing has succeeded in the configured
   window.

A window exists for rule 2 to insulate from automations failing for a legit
period of time due to outages, etc.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.config import get_config
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.models import (
    Automation,
    AutomationDisableEvent,
    AutomationRun,
    AutomationRunStatus,
    AutomationState,
)
from openhands.automation.utils.run import skip_pending_runs_for_disabled_automation
from openhands.automation.utils.time import ensure_utc, utcnow


logger = logging.getLogger(__name__)

PERMANENT_FAILURE_KINDS = frozenset({"auth", "config", "quota", "blocked"})
TERMINAL_OUTCOME_STATUSES = (
    AutomationRunStatus.COMPLETED,
    AutomationRunStatus.FAILED,
)

SOURCE_PERMANENT = "consecutive_permanent_failures"
SOURCE_CONSECUTIVE = "consecutive_failures"


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


def _humanize(hours: float) -> str:
    if hours < 48:
        return f"{round(hours)} hours"
    return f"{round(hours / 24)} days"


@dataclass(frozen=True)
class _DisableCause:
    """Why an automation is being auto-disabled, and what to record about it."""

    reason: str
    source: str
    detail: dict[str, Any]
    run_id: uuid.UUID | None


async def _history_boundary(
    session: AsyncSession, automation_id: uuid.UUID
) -> datetime | None:
    """Timestamp before which run history is ignored.

    Re-enabling an automation leaves its old failures in scope, so without
    this the very next failure disables it again immediately. A
    re-enable always follows a disable event, so the latest event is the
    boundary.
    """
    return await session.scalar(
        select(func.max(AutomationDisableEvent.created_at)).where(
            AutomationDisableEvent.automation_id == automation_id
        )
    )


async def _recent_terminal_runs(
    session: AsyncSession,
    automation_id: uuid.UUID,
    boundary: datetime | None,
    limit: int,
) -> Sequence[AutomationRun]:
    """The automation's most recent terminal runs, newest first."""
    stmt = select(AutomationRun).where(
        AutomationRun.automation_id == automation_id,
        AutomationRun.status.in_(TERMINAL_OUTCOME_STATUSES),
    )
    if boundary is not None:
        stmt = stmt.where(AutomationRun.created_at > boundary)
    result = await session.execute(
        stmt.order_by(AutomationRun.created_at.desc()).limit(limit)
    )
    return result.scalars().all()


async def _succeeded_since(
    session: AsyncSession,
    automation_id: uuid.UUID,
    since: datetime,
) -> bool:
    """Whether any run has completed since `since`. Stops at the first hit."""
    found = await session.scalar(
        select(AutomationRun.id)
        .where(
            AutomationRun.automation_id == automation_id,
            AutomationRun.status == AutomationRunStatus.COMPLETED,
            AutomationRun.created_at > since,
        )
        .limit(1)
    )
    return found is not None


def _leading_failures(runs: Sequence[AutomationRun]) -> int:
    count = 0
    for run in runs:
        if run.status != AutomationRunStatus.FAILED:
            break
        count += 1
    return count


def _check_permanent(
    runs: Sequence[AutomationRun],
    threshold: int,
) -> _DisableCause | None:
    """Fire on consecutive terminal runs that all carry permanent-fault detail."""
    if threshold <= 0 or len(runs) < threshold:
        return None

    count = 0
    for run in runs:
        if not is_permanent_failure_detail(run.status_detail):
            break
        count += 1
    if count < threshold:
        return None

    detail = runs[0].status_detail or {}
    formatted = _string_value(detail.get("formatted_detail"))
    message = formatted or _string_value(detail.get("detail")) or "unknown error"
    kind = _string_value(detail.get("kind")) or "permanent_failure"
    return _DisableCause(
        reason=(
            f"Paused automatically: {kind} — {message}. "
            f"This failed the last {count} runs and needs a configuration fix."
        ),
        source=SOURCE_PERMANENT,
        detail={
            "rule": SOURCE_PERMANENT,
            "threshold": threshold,
            "consecutive_permanent_failures": count,
            "status_detail": detail,
        },
        run_id=runs[0].id,
    )


def _consecutive_cause(
    runs: Sequence[AutomationRun],
    threshold: int,
    window_hours: float,
) -> _DisableCause:
    count = _leading_failures(runs)
    return _DisableCause(
        reason=(
            f"Paused automatically: the last {count} runs all failed and nothing "
            f"has succeeded in {_humanize(window_hours)}."
        ),
        source=SOURCE_CONSECUTIVE,
        detail={
            "rule": SOURCE_CONSECUTIVE,
            "threshold": threshold,
            "consecutive_failures": count,
            "window_hours": window_hours,
        },
        run_id=runs[0].id,
    )


async def _apply_disable(
    session: AsyncSession,
    automation_id: uuid.UUID,
    cause: _DisableCause,
) -> bool:
    disabled_detail = {
        "reason": cause.reason,
        "source": cause.source,
        "run_id": str(cause.run_id) if cause.run_id else None,
        **cause.detail,
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
            lifecycle_status=AutomationState.INACTIVE,
            disabled_reason=cause.reason,
            disabled_detail=disabled_detail,
            disabled_at=disabled_at,
        )
    )
    if result.rowcount == 0:
        return False

    await skip_pending_runs_for_disabled_automation(
        session,
        automation_id,
        reason=cause.reason,
        disabled_detail=disabled_detail,
        completed_at=disabled_at,
    )

    automation = await session.get(Automation, automation_id)
    if automation is not None:
        await mark_git_sync_dirty(session, automation)

    session.add(
        AutomationDisableEvent(
            automation_id=automation_id,
            run_id=cause.run_id,
            reason=cause.reason,
            detail=disabled_detail,
            source=cause.source,
        )
    )

    logger.warning(
        "Automation auto-disabled (%s): %s",
        cause.source,
        cause.reason,
        extra={
            "automation_id": str(automation_id),
            "run_id": str(cause.run_id) if cause.run_id else None,
        },
    )
    return True


async def maybe_disable_unhealthy_automation(
    session: AsyncSession,
    automation_id: uuid.UUID,
    *,
    threshold: int | None = None,
) -> bool:
    """Disable an automation if either auto-disable rule has fired.

    Args:
        threshold: Override for the permanent-failure rule only; the
            consecutive rule always reads its config values.
    """
    service = get_config().service
    if threshold is None:
        threshold = service.failure_disable_threshold
    threshold = max(threshold, 0)
    consecutive = max(service.consecutive_failure_disable_threshold or 0, 0)
    window_hours = service.consecutive_failure_disable_window_hours

    limit = max(threshold, consecutive)
    if limit <= 0:
        return False

    boundary = await _history_boundary(session, automation_id)
    runs = await _recent_terminal_runs(session, automation_id, boundary, limit)

    cause = _check_permanent(runs, threshold)

    # The all-failed check is in memory, so the success lookup only runs for
    # automations that are already failing straight through.
    if cause is None and consecutive > 0 and _leading_failures(runs) >= consecutive:
        since = utcnow() - timedelta(hours=window_hours)
        if boundary is not None:
            since = max(since, ensure_utc(boundary))
        if not await _succeeded_since(session, automation_id, since):
            cause = _consecutive_cause(runs, consecutive, window_hours)

    if cause is None:
        return False
    return await _apply_disable(session, automation_id, cause)


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
