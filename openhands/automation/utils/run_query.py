"""Shared query helpers for listing and exporting automation runs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import AutomationRun, AutomationRunStatus


def effective_run_start_column():
    """Wall-clock start used for date filters: started_at, else created_at."""
    return func.coalesce(AutomationRun.started_at, AutomationRun.created_at)


def automation_runs_filter(
    automation_id: uuid.UUID,
    *,
    statuses: list[AutomationRunStatus] | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> list:
    """Build WHERE clauses shared by list and export endpoints."""
    clauses: list = [AutomationRun.automation_id == automation_id]

    if statuses:
        clauses.append(AutomationRun.status.in_(statuses))

    start_col = effective_run_start_column()
    if started_after is not None:
        clauses.append(start_col >= started_after)
    if started_before is not None:
        clauses.append(start_col <= started_before)

    return clauses


def select_automation_runs(
    automation_id: uuid.UUID,
    *,
    statuses: list[AutomationRunStatus] | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> Select[tuple[AutomationRun]]:
    """Select runs for an automation with optional filters, latest first."""
    clauses = automation_runs_filter(
        automation_id,
        statuses=statuses,
        started_after=started_after,
        started_before=started_before,
    )
    return (
        select(AutomationRun)
        .where(and_(*clauses))
        .order_by(AutomationRun.created_at.desc())
    )


async def count_automation_runs(
    session: AsyncSession,
    automation_id: uuid.UUID,
    *,
    statuses: list[AutomationRunStatus] | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> int:
    """Count runs matching the same filters as :func:`select_automation_runs`."""
    clauses = automation_runs_filter(
        automation_id,
        statuses=statuses,
        started_after=started_after,
        started_before=started_before,
    )
    result = await session.execute(
        select(func.count()).select_from(AutomationRun).where(and_(*clauses))
    )
    return result.scalar() or 0
