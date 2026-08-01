"""Shared query helpers for listing and exporting automation runs."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import AutomationRun


def select_automation_runs(automation_id: uuid.UUID) -> Select[tuple[AutomationRun]]:
    """Select runs for an automation, latest first."""
    return (
        select(AutomationRun)
        .where(AutomationRun.automation_id == automation_id)
        .order_by(AutomationRun.created_at.desc())
    )


async def count_automation_runs(
    session: AsyncSession,
    automation_id: uuid.UUID,
) -> int:
    """Count runs for an automation."""
    result = await session.execute(
        select(func.count())
        .select_from(AutomationRun)
        .where(AutomationRun.automation_id == automation_id)
    )
    return result.scalar() or 0
