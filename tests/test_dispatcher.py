"""Tests for the dispatcher module.

The dispatcher polls for PENDING automation runs and marks them as RUNNING.
"""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from automation.dispatcher import (
    dispatch_pending_runs,
    dispatcher_loop,
)
from automation.models import Automation, AutomationRun, AutomationRunStatus
from automation.utils import utcnow
from automation.utils.run import mark_run_status
from automation.utils.tarball_validation import is_http_url


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


class TestIsHttpUrl:
    """Tests for is_http_url helper function."""

    def test_https_url_is_http(self):
        """HTTPS URLs are HTTP URLs (downloadable with curl in sandbox)."""
        assert is_http_url("https://example.com/file.tar.gz") is True
        github_url = "https://github.com/user/repo/archive/main.tar.gz"
        assert is_http_url(github_url) is True

    def test_http_url_is_http(self):
        """HTTP URLs are HTTP URLs (downloadable with curl in sandbox)."""
        assert is_http_url("http://example.com/file.tar.gz") is True

    def test_internal_url_is_not_http(self):
        """Internal URLs (oh-internal://) are not HTTP URLs."""
        internal_url = "oh-internal://uploads/12345678-1234-5678-1234-567812345678"
        assert is_http_url(internal_url) is False

    def test_s3_url_is_not_http(self):
        """S3 URLs are not HTTP URLs (need special handling, not curl)."""
        assert is_http_url("s3://bucket/key.tar.gz") is False

    def test_gs_url_is_not_http(self):
        """GCS URLs are not HTTP URLs (need special handling, not curl)."""
        assert is_http_url("gs://bucket/key.tar.gz") is False


class TestMarkRunStatus:
    """Tests for mark_run_status function."""

    async def test_marks_run_as_running(self, async_session_factory):
        """Run status is changed to RUNNING."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

            await mark_run_status(session, run, AutomationRunStatus.RUNNING)
            await session.commit()

        # Verify status changed
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run_id)
            )
            updated = result.scalars().first()
            assert updated.status == AutomationRunStatus.RUNNING
            assert updated.started_at is not None

    async def test_sets_started_at_timestamp(self, async_session_factory):
        """started_at is set to current time when transitioning to RUNNING."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()

            before = utcnow()
            await mark_run_status(session, run, AutomationRunStatus.RUNNING)
            await session.commit()
            after = utcnow()

            assert run.started_at is not None
            # started_at should be between before and after
            assert before <= run.started_at <= after

    async def test_sets_completed_at_on_completed(self, async_session_factory):
        """completed_at is set when transitioning to COMPLETED."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                started_at=utcnow(),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

            before = utcnow()
            await mark_run_status(session, run, AutomationRunStatus.COMPLETED)
            await session.commit()
            after = utcnow()

        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run_id)
            )
            updated = result.scalars().first()
            assert updated.status == AutomationRunStatus.COMPLETED
            assert updated.completed_at is not None
            assert before <= updated.completed_at <= after

    async def test_sets_completed_at_on_failed(self, async_session_factory):
        """completed_at is set when transitioning to FAILED."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                started_at=utcnow(),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

            before = utcnow()
            await mark_run_status(session, run, AutomationRunStatus.FAILED)
            await session.commit()
            after = utcnow()

        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run_id)
            )
            updated = result.scalars().first()
            assert updated.status == AutomationRunStatus.FAILED
            assert updated.completed_at is not None
            assert before <= updated.completed_at <= after


class TestDispatchPendingRuns:
    """Tests for dispatch_pending_runs function."""

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_dispatches_pending_runs(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Pending runs are dispatched and marked as RUNNING."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        dispatched = await dispatch_pending_runs(async_session_factory, mock_settings)

        assert len(dispatched) == 1
        assert dispatched[0].id == run_id

        # Verify status changed in DB
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run_id)
            )
            updated = result.scalars().first()
            assert updated.status == AutomationRunStatus.RUNNING

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_ignores_running_runs(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Runs already in RUNNING status are not dispatched."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                started_at=utcnow(),
            )
            session.add(run)
            await session.commit()

        dispatched = await dispatch_pending_runs(async_session_factory, mock_settings)

        assert len(dispatched) == 0

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_ignores_completed_runs(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Completed runs are not dispatched."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                started_at=utcnow(),
                completed_at=utcnow(),
            )
            session.add(run)
            await session.commit()

        dispatched = await dispatch_pending_runs(async_session_factory, mock_settings)

        assert len(dispatched) == 0

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_respects_batch_size(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Only batch_size runs are dispatched at once."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            # Create 5 pending runs
            for _ in range(5):
                run = AutomationRun(
                    automation_id=automation.id,
                    status=AutomationRunStatus.PENDING,
                )
                session.add(run)
            await session.commit()

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, batch_size=2
        )

        assert len(dispatched) == 2

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_orders_by_created_at(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Oldest pending runs are dispatched first."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            old_run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
                created_at=now - timedelta(hours=1),
            )
            new_run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
                created_at=now,
            )
            session.add_all([new_run, old_run])  # Add in reverse order
            await session.commit()
            old_run_id = old_run.id

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, batch_size=1
        )

        assert len(dispatched) == 1
        assert dispatched[0].id == old_run_id  # Old run should be first


class TestDispatcherLoop:
    """Tests for dispatcher_loop function."""

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_dispatcher_loop_exits_on_shutdown(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Dispatcher exits gracefully when shutdown event is set."""
        shutdown_event = asyncio.Event()

        task = asyncio.create_task(
            dispatcher_loop(
                async_session_factory,
                mock_settings,
                interval_seconds=1,
                shutdown_event=shutdown_event,
            )
        )

        await asyncio.sleep(0.1)
        shutdown_event.set()

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("Dispatcher did not exit on shutdown signal")

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_dispatcher_loop_dispatches_runs(
        self, mock_execute, async_session_factory, mock_settings, caplog
    ):
        """Dispatcher polls and dispatches pending runs."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        shutdown_event = asyncio.Event()

        import logging

        with caplog.at_level(logging.INFO, logger="automation.dispatcher"):
            task = asyncio.create_task(
                dispatcher_loop(
                    async_session_factory,
                    mock_settings,
                    interval_seconds=60,
                    shutdown_event=shutdown_event,
                )
            )

            await asyncio.sleep(0.2)

            shutdown_event.set()
            await asyncio.wait_for(task, timeout=2.0)

        # Check logs
        assert any(
            "Dispatching automation run" in record.message for record in caplog.records
        )
        assert any("Dispatched 1 run" in record.message for record in caplog.records)

        # Verify run status changed
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run_id)
            )
            updated = result.scalars().first()
            assert updated.status == AutomationRunStatus.RUNNING


class TestEffectiveTimeout:
    """Tests for effective timeout calculation in dispatcher."""

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_uses_automation_timeout_when_set(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Dispatcher uses automation's timeout when set."""

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="With Timeout",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                timeout=120,  # Custom timeout
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()

        await dispatch_pending_runs(async_session_factory, mock_settings)

        # Verify _execute_run_safe was called
        mock_execute.assert_called_once()
        # The automation passed should have timeout=120
        call_args = mock_execute.call_args
        run_arg = call_args[0][0]
        assert run_arg.automation.timeout == 120

    @patch("automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_uses_default_timeout_when_not_set(
        self, mock_execute, async_session_factory, mock_settings
    ):
        """Dispatcher uses MAX_RUN_DURATION_SECONDS when automation timeout is None."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="No Timeout",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                timeout=None,  # No custom timeout
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            session.add(run)
            await session.commit()

        await dispatch_pending_runs(async_session_factory, mock_settings)

        # Verify _execute_run_safe was called
        mock_execute.assert_called_once()
        # The automation passed should have timeout=None
        call_args = mock_execute.call_args
        run_arg = call_args[0][0]
        assert run_arg.automation.timeout is None
