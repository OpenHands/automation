"""Unit tests for shared automation run query helpers."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.run_query import (
    count_automation_runs,
    select_automation_runs,
)
from openhands.automation.utils.time import utcnow


@pytest.mark.asyncio
async def test_count_and_select_respect_status_filter(async_session: AsyncSession):
    automation = Automation(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Query Helper",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )
    async_session.add(automation)
    await async_session.flush()

    async_session.add_all(
        [
            AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.FAILED,
            ),
            AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
            ),
        ]
    )
    await async_session.commit()

    total = await count_automation_runs(
        async_session,
        automation.id,
        statuses=[AutomationRunStatus.FAILED],
    )
    assert total == 1

    result = await async_session.execute(
        select_automation_runs(
            automation.id,
            statuses=[AutomationRunStatus.FAILED],
        )
    )
    runs = list(result.scalars().all())
    assert len(runs) == 1
    assert runs[0].status == AutomationRunStatus.FAILED


@pytest.mark.asyncio
async def test_started_after_uses_coalesced_start(async_session: AsyncSession):
    automation = Automation(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Date Filter",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )
    async_session.add(automation)
    await async_session.flush()

    now = utcnow()
    async_session.add_all(
        [
            AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                started_at=now - timedelta(days=3),
                created_at=now - timedelta(days=3),
            ),
            AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                started_at=None,
                created_at=now - timedelta(hours=1),
            ),
        ]
    )
    await async_session.commit()

    total = await count_automation_runs(
        async_session,
        automation.id,
        started_after=now - timedelta(days=1),
    )
    assert total == 1
