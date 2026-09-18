"""Tests for the cancel run endpoint."""

import uuid

from sqlalchemy import select

from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils import utcnow


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


async def _create_automation_with_run(
    session, status=AutomationRunStatus.PENDING, sandbox_id=None
):
    """Helper to create an automation with a run in the given status."""
    automation = Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Test Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
    )
    session.add(automation)
    await session.flush()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=status,
        sandbox_id=sandbox_id,
    )
    if status == AutomationRunStatus.RUNNING:
        run.started_at = utcnow()
    session.add(run)
    await session.flush()
    return automation, run


async def test_cancel_pending_run(async_client, async_session):
    """Cancelling a PENDING run should succeed."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.PENDING
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CANCELLED"
    assert data["error_detail"] == "Cancelled by user"
    assert data["completed_at"] is not None


async def test_cancel_running_run(async_client, async_session):
    """Cancelling a RUNNING run should succeed."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.RUNNING
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CANCELLED"


async def test_cancel_completed_run_returns_409(async_client, async_session):
    """Cancelling a COMPLETED run should return 409."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.COMPLETED
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 409
    assert "COMPLETED" in resp.json()["detail"]


async def test_cancel_failed_run_returns_409(async_client, async_session):
    """Cancelling a FAILED run should return 409."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.FAILED
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 409
    assert "FAILED" in resp.json()["detail"]


async def test_cancel_already_cancelled_run_returns_409(async_client, async_session):
    """Cancelling an already-cancelled run should return 409."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.CANCELLED
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 409
    assert "CANCELLED" in resp.json()["detail"]


async def test_cancel_nonexistent_run_returns_404(async_client, async_session):
    """Cancelling a non-existent run should return 404."""
    fake_id = uuid.uuid4()
    resp = await async_client.post(f"/api/automation/v1/runs/{fake_id}/cancel")
    assert resp.status_code == 404


async def test_cancel_other_orgs_run_returns_403(async_client, async_session):
    """Cancelling a run from another org should return 403."""
    automation = Automation(
        user_id=OTHER_USER_ID,
        org_id=OTHER_ORG_ID,
        name="Other User Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
    )
    async_session.add(run)
    await async_session.flush()

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 403


async def test_cancel_subject_run_without_sandbox_releases_subject(
    async_client, async_session
):
    """Cancelling a subject run with no sandbox still releases the subject."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.RUNNING
    )
    run.subject_key = "team/C123/1755000000.000100"
    await async_session.commit()

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200

    await async_session.refresh(run)
    assert run.status == AutomationRunStatus.CANCELLED
    assert run.subject_released_at is not None
    assert run.subject_key == "team/C123/1755000000.000100"


async def test_cancel_ordinary_run_touches_no_subject(async_client, async_session):
    """Cancelling a run without a subject leaves subject columns alone."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.RUNNING
    )

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200

    await async_session.refresh(run)
    assert run.status == AutomationRunStatus.CANCELLED
    assert run.subject_key is None
    assert run.subject_released_at is None


async def test_cancelled_subject_no_longer_blocks_resubmission(
    async_client, async_session
):
    """After cancel, no unreleased run holds the subject, so a resubmission
    routes to a new run instead of deduplicating against the cancelled one."""
    _, run = await _create_automation_with_run(
        async_session, status=AutomationRunStatus.RUNNING
    )
    run.subject_key = "team/C123/1755000000.000100"
    await async_session.commit()

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200

    holders = (
        (
            await async_session.execute(
                select(AutomationRun).where(
                    AutomationRun.subject_key == "team/C123/1755000000.000100",
                    AutomationRun.subject_released_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert holders == []


async def test_cancel_same_org_other_users_run(async_client, async_session):
    """Cancelling a run owned by another member of the same org should succeed."""
    automation = Automation(
        user_id=OTHER_USER_ID,
        org_id=TEST_ORG_ID,
        name="Teammate Automation",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="https://example.com/code.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()

    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
        started_at=utcnow(),
    )
    async_session.add(run)
    await async_session.flush()

    resp = await async_client.post(f"/api/automation/v1/runs/{run.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
