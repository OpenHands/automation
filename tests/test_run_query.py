"""Unit tests for shared automation run query helpers."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.run_query import (
    count_automation_runs,
    select_automation_runs,
)


@pytest.mark.asyncio
async def test_count_and_select_by_automation_id(async_session: AsyncSession):
    automation = Automation(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Query Helper",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="uv run script.py",
    )
    other = Automation(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Other",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/other.tar.gz",
        entrypoint="uv run script.py",
    )
    async_session.add_all([automation, other])
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
            AutomationRun(
                automation_id=other.id,
                status=AutomationRunStatus.COMPLETED,
            ),
        ]
    )
    await async_session.commit()

    total = await count_automation_runs(async_session, automation.id)
    assert total == 2

    result = await async_session.execute(select_automation_runs(automation.id))
    runs = list(result.scalars().all())
    assert len(runs) == 2
    assert {run.status for run in runs} == {
        AutomationRunStatus.FAILED,
        AutomationRunStatus.COMPLETED,
    }
