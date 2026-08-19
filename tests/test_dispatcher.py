"""Tests for the dispatcher module.

The dispatcher polls for PENDING automation runs and marks them as RUNNING.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation import dispatcher
from openhands.automation.config import get_config
from openhands.automation.dispatcher import (
    _build_event_payload,
    _execute_run,
    dispatch_pending_runs,
    dispatcher_loop,
)
from openhands.automation.exceptions import ConcurrencyLimitReachedError
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils import utcnow
from openhands.automation.utils.run import (
    mark_run_status,
    mark_run_terminal,
    update_run_timeout_at,
)
from openhands.automation.utils.tarball_validation import is_http_url


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
def mock_client():
    """Mock httpx.AsyncClient for tests."""
    return MagicMock()


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


class TestUpdateRunTimeoutAt:
    """Tests for the RUNNING-guarded watchdog-deadline reset."""

    async def test_does_not_resurrect_terminal_run_deadline(
        self, async_session_factory
    ):
        """A run that reached a terminal state keeps its original deadline."""
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

            original_timeout_at = utcnow() + timedelta(minutes=5)
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                started_at=utcnow(),
                completed_at=utcnow(),
                timeout_at=original_timeout_at,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        await update_run_timeout_at(
            async_session_factory, run_id, utcnow() + timedelta(hours=1)
        )

        async with async_session_factory() as session:
            updated = await session.get(AutomationRun, run_id)
            assert updated.timeout_at == original_timeout_at


class TestMarkRunTerminalFirstRunOutcome:
    """First-run outcome recording when the dispatcher terminates a run."""

    async def _seed_template_run(self, async_session_factory):
        """A RUNNING run on an automation created from an extension template."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Template Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                preset_metadata={
                    "preset_type": "prompt",
                    "prompt": "p",
                    "template": {"id": "tpl", "version": "1.0.0"},
                },
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
            )
            session.add(run)
            await session.commit()
            return automation.id, run

    async def test_dispatch_failure_records_dispatch_stage(self, async_session_factory):
        """A run failed at dispatch records the dispatch failure stage."""
        automation_id, run = await self._seed_template_run(async_session_factory)

        await mark_run_terminal(
            async_session_factory,
            run,
            AutomationRunStatus.FAILED,
            "sandbox creation failed",
        )

        async with async_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            first_run = automation.preset_metadata["first_run"]
            assert first_run["status"] == "failure"
            assert first_run["failure_stage"] == "dispatch"

    async def test_skipped_run_does_not_consume_the_first_run_slot(
        self, async_session_factory
    ):
        """A skipped run records nothing, so a later real run still can."""
        automation_id, run = await self._seed_template_run(async_session_factory)

        await mark_run_terminal(async_session_factory, run, AutomationRunStatus.SKIPPED)

        async with async_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert "first_run" not in automation.preset_metadata


class TestDispatchPendingRuns:
    """Tests for dispatch_pending_runs function."""

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_dispatches_pending_runs(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, mock_client
        )

        assert len(dispatched) == 1
        assert dispatched[0].id == run_id

        # Verify status changed in DB
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(AutomationRun.id == run_id)
            )
            updated = result.scalars().first()
            assert updated.status == AutomationRunStatus.RUNNING

    @patch(
        "openhands.automation.dispatcher.capture_automation_event",
        new_callable=AsyncMock,
    )
    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_dispatch_emits_single_run_lifecycle_event(
        self,
        mock_execute,
        mock_capture_event,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """Dispatch is the canonical telemetry event for a run starting."""
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

        await dispatch_pending_runs(async_session_factory, mock_settings, mock_client)

        emitted_events = [call.args[0] for call in mock_capture_event.await_args_list]
        assert emitted_events == ["automation_run_dispatched"]

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_ignores_running_runs(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, mock_client
        )

        assert len(dispatched) == 0

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_ignores_completed_runs(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, mock_client
        )

        assert len(dispatched) == 0

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_respects_batch_size(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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
            async_session_factory, mock_settings, mock_client, batch_size=2
        )

        assert len(dispatched) == 2

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_orders_by_created_at(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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
            async_session_factory, mock_settings, mock_client, batch_size=1
        )

        assert len(dispatched) == 1
        assert dispatched[0].id == old_run_id  # Old run should be first


class TestDispatcherLoop:
    """Tests for dispatcher_loop function."""

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_dispatcher_loop_exits_on_shutdown(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
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

        with caplog.at_level(logging.INFO, logger="openhands.automation.dispatcher"):
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

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_uses_automation_timeout_when_set(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        """Dispatcher uses automation's timeout when set, even above default."""

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="With Timeout",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                timeout=1200,
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

        await dispatch_pending_runs(async_session_factory, mock_settings, mock_client)

        # Verify _execute_run_safe was called
        mock_execute.assert_called_once()
        # The automation passed should have timeout=1200
        call_args = mock_execute.call_args
        run_arg = call_args[0][0]
        assert run_arg.automation.timeout == 1200

        async with async_session_factory() as session:
            updated_run = await session.get(AutomationRun, run_id)
            assert updated_run is not None
            assert updated_run.timeout_at is not None
            assert updated_run.started_at is not None
            # Phase-1 provisioning deadline: run budget padded with the
            # sandbox-ready budget and margin (reset at bash start).
            sandbox_cfg = get_config().sandbox
            assert (
                updated_run.timeout_at - updated_run.started_at
            ).total_seconds() == (
                1200
                + sandbox_cfg.sandbox_ready_timeout
                + sandbox_cfg.run_timeout_margin
            )

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_uses_default_timeout_when_not_set(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        """Dispatcher uses default_run_duration when automation timeout is None."""
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
            run_id = run.id

        await dispatch_pending_runs(async_session_factory, mock_settings, mock_client)

        # Verify _execute_run_safe was called
        mock_execute.assert_called_once()
        # The automation passed should have timeout=None
        call_args = mock_execute.call_args
        run_arg = call_args[0][0]
        assert run_arg.automation.timeout is None

        async with async_session_factory() as session:
            updated_run = await session.get(AutomationRun, run_id)
            assert updated_run is not None
            assert updated_run.timeout_at is not None
            assert updated_run.started_at is not None
            # Phase-1 provisioning deadline: default run budget padded with
            # the sandbox-ready budget and margin (reset at bash start).
            sandbox_cfg = get_config().sandbox
            assert (
                updated_run.timeout_at - updated_run.started_at
            ).total_seconds() == (
                sandbox_cfg.default_run_duration
                + sandbox_cfg.sandbox_ready_timeout
                + sandbox_cfg.run_timeout_margin
            )

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_successful_dispatch_resets_timeout_to_bash_start(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        """Once the bash command starts, timeout_at is re-anchored to bash
        start + run budget + margin, dropping the provisioning padding."""
        sandbox_cfg = get_config().sandbox
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Reset Timeout",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="https://example.com/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
                timeout=None,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                started_at=now,
                timeout_at=now
                + timedelta(
                    seconds=sandbox_cfg.default_run_duration
                    + sandbox_cfg.sandbox_ready_timeout
                    + sandbox_cfg.run_timeout_margin
                ),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        async with async_session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(AutomationRun)
                        .options(selectinload(AutomationRun.automation))
                        .where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .first()
            )

        backend = MagicMock()
        ctx = MagicMock(
            agent_url="http://agent.test", sandbox_id="sbx-1", session_key="sk-1"
        )
        backend.get_execution_context = AsyncMock(return_value=ctx)
        backend.build_env_vars = MagicMock(return_value={})
        backend.get_work_dir = MagicMock(return_value="/workspace")
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        async with async_session_factory() as session:
            updated = await session.get(AutomationRun, run_id)
            assert updated.status == AutomationRunStatus.RUNNING
            # Re-anchored to bash start: provisioning padding dropped.
            remaining = (updated.timeout_at - utcnow()).total_seconds()
            expected = sandbox_cfg.default_run_duration + sandbox_cfg.run_timeout_margin
            assert expected - 30 < remaining <= expected


class TestBuildEventPayload:
    """Tests for _build_event_payload — ensures generated payloads produce
    tag-safe trigger values (≤256 chars) while preserving the full trigger
    dict in trigger_payload for downstream consumers.

    See: https://github.com/OpenHands/automation/issues/111
    """

    def _make_automation(self, trigger: dict[str, Any] | None, **kw: Any) -> Automation:
        defaults = dict(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
        )
        defaults.update(kw)
        return Automation(trigger=cast(Any, trigger), **defaults)

    def _make_run(self, automation: Automation, **kw) -> AutomationRun:
        return AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.PENDING,
            **kw,
        )

    def test_cron_trigger_uses_type_string(self):
        """Cron trigger → payload['trigger'] == 'cron' (not the full dict)."""
        trigger = {"type": "cron", "schedule": "0 9 * * 5", "timezone": "UTC"}
        automation = self._make_automation(trigger)
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        assert payload["trigger"] == "cron"
        assert payload["trigger_payload"] == trigger
        assert payload["automation_name"] == "Test"

    def test_event_trigger_uses_type_string(self):
        """Event trigger preserves full dict in trigger_payload."""
        trigger = {
            "type": "event",
            "source": "github",
            "on": ["pull_request.labeled", "issues.labeled"],
            "filter": (
                "repository.full_name == 'OpenHands/software-agent-sdk' "
                "&& label.name == 'oh-cloud-review' "
                "&& (pull_request.number != null || issue.pull_request.url != null)"
            ),
        }
        automation = self._make_automation(trigger)
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        assert payload["trigger"] == "event"
        assert payload["trigger_payload"] == trigger
        assert payload["trigger_payload"]["source"] == "github"
        assert payload["trigger_payload"]["filter"] == trigger["filter"]
        # The trigger value must fit in a 256-char tag
        assert len(str(payload["trigger"])) <= 256

    def test_long_filter_does_not_exceed_tag_limit(self):
        """A very long filter still produces a short tag value."""
        long_filter = " && ".join([f"field_{i} == 'value_{i}'" for i in range(50)])
        trigger = {
            "type": "event",
            "source": "github",
            "on": "issue_comment.created",
            "filter": long_filter,
        }
        automation = self._make_automation(trigger)
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        # The full trigger dict string would be >256 chars
        assert len(str(trigger)) > 256
        # But payload['trigger'] is just the type string
        assert payload["trigger"] == "event"
        assert len(payload["trigger"]) <= 256
        # Full dict is still available in trigger_payload
        assert payload["trigger_payload"] == trigger

    def test_event_payload_included_when_present(self):
        """Run event_payload is passed through as 'event' key."""
        trigger = {"type": "event", "source": "github", "on": "push"}
        automation = self._make_automation(trigger)
        event_data = {"action": "push", "ref": "refs/heads/main"}
        run = self._make_run(automation, event_payload=event_data)

        payload = _build_event_payload(automation, run)

        assert payload["event"] == event_data

    def test_event_payload_omitted_when_none(self):
        """No 'event' key when run has no event_payload."""
        trigger = {"type": "cron", "schedule": "0 0 * * *", "timezone": "UTC"}
        automation = self._make_automation(trigger)
        run = self._make_run(automation, event_payload=None)

        payload = _build_event_payload(automation, run)

        assert "event" not in payload

    def test_model_included_when_present(self):
        """Automation model is passed through for preset scripts."""
        trigger = {"type": "cron", "schedule": "0 0 * * *", "timezone": "UTC"}
        automation = self._make_automation(trigger, model="fast-profile")
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        assert payload["model"] == "fast-profile"

    def test_none_trigger_defaults_to_unknown(self):
        """None trigger → 'unknown' type, trigger_payload is None."""
        automation = self._make_automation(trigger=None)
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        assert payload["trigger"] == "unknown"
        assert payload["trigger_payload"] is None

    def test_empty_dict_trigger(self):
        """Empty dict trigger → 'unknown' type, trigger_payload is empty dict."""
        automation = self._make_automation(trigger={})
        automation.trigger = {}
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        assert payload["trigger"] == "unknown"
        assert payload["trigger_payload"] == {}


class TestExecuteRunConcurrencyLimit:
    """When the org/workspace is at its concurrent-sandbox limit, the run is
    marked SKIPPED (not FAILED) and the automation is left enabled."""

    async def _make_running_run(self, async_session_factory):
        """Create an automation + a RUNNING run (as the dispatcher leaves it
        right before calling get_execution_context), with the automation
        relationship eagerly loaded for _execute_run."""
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
            automation_id = automation.id

        async with async_session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(AutomationRun)
                        .options(selectinload(AutomationRun.automation))
                        .where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .first()
            )
        return run, run_id, automation_id

    async def test_concurrency_limit_marks_skipped_and_keeps_enabled(
        self, async_session_factory, mock_settings, mock_client
    ):
        """A ConcurrencyLimitReachedError from get_execution_context marks the
        run SKIPPED (with completed_at, no error_detail) and does NOT disable
        the automation."""
        run, run_id, automation_id = await self._make_running_run(async_session_factory)

        backend = MagicMock()
        backend.is_local_mode = False
        backend.get_execution_context = AsyncMock(
            side_effect=ConcurrencyLimitReachedError(
                "You have reached your limit of 3 concurrent conversations."
            )
        )
        backend.release_context = AsyncMock()

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        async with async_session_factory() as session:
            updated = (
                (
                    await session.execute(
                        select(AutomationRun).where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .first()
            )
            assert updated.status == AutomationRunStatus.SKIPPED
            assert updated.completed_at is not None
            assert updated.status_detail is not None
            assert updated.status_detail["phase"] == "dispatch"
            assert updated.status_detail["kind"] == "concurrency_limit"
            assert updated.status_detail["transient"] is True
            assert updated.error_detail is None  # SKIPPED is not a failure

            auto = (
                (
                    await session.execute(
                        select(Automation).where(Automation.id == automation_id)
                    )
                )
                .scalars()
                .first()
            )
            assert auto.enabled is True  # transient org-level condition: not disabled

        # No execution context was acquired, so there is nothing to release.
        backend.release_context.assert_not_called()

    async def test_generic_context_failure_still_marks_failed(
        self, async_session_factory, mock_settings, mock_client
    ):
        """Regression: a non-concurrency failure in get_execution_context still
        marks the run FAILED — the new SKIPPED branch must not swallow it."""
        run, run_id, _ = await self._make_running_run(async_session_factory)

        backend = MagicMock()
        backend.is_local_mode = False
        backend.get_execution_context = AsyncMock(side_effect=RuntimeError("boom"))
        backend.release_context = AsyncMock()

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        async with async_session_factory() as session:
            updated = (
                (
                    await session.execute(
                        select(AutomationRun).where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .first()
            )
            assert updated.status == AutomationRunStatus.FAILED
            assert updated.error_detail == "Failed to get execution context"
            assert updated.status_detail is not None
            assert updated.status_detail["phase"] == "dispatch"
            assert updated.status_detail["kind"] == "unknown"
            assert updated.status_detail["source"] == "sandbox_api"
            assert updated.status_detail["operation"] == "get_execution_context"
            assert updated.status_detail["transient"] is False


class TestServicePhases:
    """A run, from being queued through entrypoint start, records the
    four service phases, in order, with a non-decreasing phase_updated_at.

    Per S01, a phase is last-write-wins with no history table — only the
    current (code, label) pair is stored. So the sequence is only observable
    at write time. The order test hooks the existing, name-stable call sites
    (get_execution_context, _download_in_sandbox, _start_bash) and reads the
    DB row at each one — it does not know or depend on the name of whatever
    internal function performs the write.
    """

    async def _make_pending_run(self, async_session_factory) -> uuid.UUID:
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Phase Test",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="https://example.com/code.tar.gz",
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
            return run.id

    async def _reload_with_automation(
        self, async_session_factory, run_id: uuid.UUID
    ) -> AutomationRun:
        async with async_session_factory() as session:
            return (
                (
                    await session.execute(
                        select(AutomationRun)
                        .options(selectinload(AutomationRun.automation))
                        .where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .first()
            )

    def _fake_backend(self) -> MagicMock:
        backend = MagicMock()
        ctx = MagicMock(
            agent_url="http://agent.test", sandbox_id=None, session_key="sk"
        )
        backend.get_execution_context = AsyncMock(return_value=ctx)
        backend.build_env_vars = MagicMock(return_value={})
        backend.get_work_dir = MagicMock(return_value="/workspace")
        backend.release_context = AsyncMock()
        return backend

    def _wire_phase_snapshots(
        self, async_session_factory, run_id, mock_download, mock_start_bash
    ):
        """Hook the three sandbox boundaries so each records the run's phase at
        the moment it is reached, giving an ordered trace of what the dispatcher
        wrote. Returns the trace, the backend to patch in, and the snapshot
        callable for phases a test needs to sample outside those boundaries."""
        snapshots: list[tuple[Any, Any, Any]] = []

        async def snapshot() -> None:
            async with async_session_factory() as session:
                row = await session.get(AutomationRun, run_id)
                snapshots.append(
                    (row.phase_code, row.phase_label, row.phase_updated_at)
                )

        backend = self._fake_backend()
        ctx = backend.get_execution_context.return_value

        async def _get_context_with_snapshot(client):
            await snapshot()  # sandbox_provisioning
            return ctx

        backend.get_execution_context = _get_context_with_snapshot

        async def _download_with_snapshot(*args, **kwargs):
            await snapshot()  # bundle_upload

        mock_download.side_effect = _download_with_snapshot

        async def _start_bash_with_snapshot(*args, **kwargs):
            await snapshot()  # entrypoint_start
            return "cmd-1"

        mock_start_bash.side_effect = _start_bash_with_snapshot

        return snapshots, backend, snapshot

    @patch("openhands.automation.execution._start_bash", new_callable=AsyncMock)
    @patch(
        "openhands.automation.execution._download_in_sandbox", new_callable=AsyncMock
    )
    @patch("openhands.automation.execution._upload", new_callable=AsyncMock)
    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_phases_recorded_in_order_with_nondecreasing_timestamps(
        self,
        mock_execute_safe,
        mock_upload,
        mock_download,
        mock_start_bash,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """queued -> sandbox_provisioning -> bundle_upload -> entrypoint_start,
        with each captured at its natural call site, and phase_updated_at
        non-decreasing across the four snapshots."""
        run_id = await self._make_pending_run(async_session_factory)
        snapshots, backend, snapshot = self._wire_phase_snapshots(
            async_session_factory, run_id, mock_download, mock_start_bash
        )

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, mock_client
        )
        assert len(dispatched) == 1
        await snapshot()  # dispatch has returned: expect "queued"

        run = await self._reload_with_automation(async_session_factory, run_id)

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        codes = [code for code, _, _ in snapshots]
        assert codes == [
            "queued",
            "sandbox_provisioning",
            "bundle_upload",
            "entrypoint_start",
        ]

        timestamps = [ts for _, _, ts in snapshots]
        assert timestamps == sorted(timestamps)

        async with async_session_factory() as session:
            final = await session.get(AutomationRun, run_id)
            assert final.phase_code == "entrypoint_start"
            assert final.phase_label == "Starting entrypoint"
            assert final.phase_updated_at == timestamps[-1]

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_failure_during_provisioning_keeps_last_phase(
        self, mock_execute_safe, async_session_factory, mock_settings, mock_client
    ):
        """A dispatch failure between sandbox_provisioning and bundle_upload
        must leave phase_code/label at "sandbox_provisioning" — not cleared,
        not reverted to "queued"."""
        run_id = await self._make_pending_run(async_session_factory)

        dispatched = await dispatch_pending_runs(
            async_session_factory, mock_settings, mock_client
        )
        assert len(dispatched) == 1

        run = await self._reload_with_automation(async_session_factory, run_id)

        backend = MagicMock()
        backend.get_execution_context = AsyncMock(
            side_effect=RuntimeError("sandbox exploded")
        )
        backend.release_context = AsyncMock()

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        async with async_session_factory() as session:
            final = await session.get(AutomationRun, run_id)
            assert final.status == AutomationRunStatus.FAILED
            assert final.phase_code == "sandbox_provisioning"
            assert final.phase_label == "Provisioning sandbox"

    @patch("openhands.automation.execution._start_bash", new_callable=AsyncMock)
    @patch(
        "openhands.automation.execution._download_in_sandbox", new_callable=AsyncMock
    )
    @patch("openhands.automation.execution._upload", new_callable=AsyncMock)
    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_phase_write_failure_does_not_fail_the_run(
        self,
        mock_execute_safe,
        mock_upload,
        mock_download,
        mock_start_bash,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """A DB failure on the *first* phase-write attempt ("queued") must not
        raise out of dispatch, and must not stop later phases (and the run
        itself) from proceeding normally — the phase is telemetry, not
        control flow. The failure is injected by intercepting any UPDATE
        naming the public ``phase_code`` column, once, at the SQL-text level
        — an external boundary (the DB session), not the unit under test."""
        mock_start_bash.return_value = "cmd-1"
        run_id = await self._make_pending_run(async_session_factory)

        failed_once = {"done": False}
        real_factory = async_session_factory

        @asynccontextmanager
        async def guarded_session_factory():
            async with real_factory() as session:
                real_execute = session.execute

                async def guarded_execute(statement, *args, **kwargs):
                    is_phase_update = getattr(
                        statement, "is_update", False
                    ) and "phase_code" in str(statement)
                    if not failed_once["done"] and is_phase_update:
                        failed_once["done"] = True
                        raise RuntimeError("simulated phase-write DB failure")
                    return await real_execute(statement, *args, **kwargs)

                session.execute = guarded_execute
                yield session

        # The dispatcher only ever calls the factory as an async context
        # manager, which this stand-in satisfies; pyright still wants the
        # nominal ``async_sessionmaker`` type, so cast at the boundary.
        guarded_factory = cast(Any, guarded_session_factory)

        dispatched = await dispatch_pending_runs(
            guarded_factory, mock_settings, mock_client
        )
        assert len(dispatched) == 1  # dispatch succeeded despite the failed write

        run = await self._reload_with_automation(async_session_factory, run_id)
        backend = self._fake_backend()

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, guarded_factory, mock_client)

        async with async_session_factory() as session:
            final = await session.get(AutomationRun, run_id)
            # The run reached entrypoint start normally...
            assert final.bash_command_id == "cmd-1"
            # ...and later phase writes (once the guard has already fired
            # once) landed correctly — the earlier failure didn't corrupt or
            # block them.
            assert final.phase_code == "entrypoint_start"
        assert failed_once["done"] is True  # the injected failure did fire

    @patch("openhands.automation.execution._start_bash", new_callable=AsyncMock)
    @patch(
        "openhands.automation.execution._download_in_sandbox", new_callable=AsyncMock
    )
    @patch("openhands.automation.execution._upload", new_callable=AsyncMock)
    async def test_real_dispatch_to_execute_run_handoff_preserves_phase_order(
        self,
        mock_upload,
        mock_download,
        mock_start_bash,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """Drives the real dispatch_pending_runs -> create_task -> _execute_run
        handoff — unlike the other phase tests, _execute_run_safe is NOT
        mocked here, so the spawned background task genuinely races the rest
        of dispatch_pending_runs. Only the genuine sandbox/network boundaries
        are mocked (get_execution_context via the fake backend,
        _download_in_sandbox, _start_bash, _upload). The spawned task is
        found via the asyncio.all_tasks() diff and awaited directly — no
        sleep/poll loop for *that* part, so waiting for it is deterministic.

        This is the seam where a "queued" write reordered to *after*
        create_task can lose a last-write-wins race against the spawned
        task's entire chain (sandbox_provisioning -> bundle_upload ->
        entrypoint_start): if the reordered "queued" write happens to commit
        *after* entrypoint_start, the run's final observable phase is wrongly
        "queued" instead of "entrypoint_start" — exactly the kind of drift
        this test rules out. A real DB round trip is normally fast enough that
        this outcome rarely flips within a single test run, so the "queued"
        write specifically is wrapped with a bounded forced delay (via
        _record_run_phase, the exact seam named in review) — long enough for
        the spawned task's entirely-mocked chain to finish first every time,
        making the mutation's failure reliable rather than a coin flip. It
        has no effect on the correct ordering: there, "queued" is written —
        and fully committed — strictly before create_task is even called, so
        the task doesn't exist yet for the delay to race against."""
        mock_start_bash.return_value = "cmd-1"
        run_id = await self._make_pending_run(async_session_factory)
        hook_snapshots, backend, _snapshot = self._wire_phase_snapshots(
            async_session_factory, run_id, mock_download, mock_start_bash
        )

        original_record_phase = dispatcher._record_run_phase

        async def _record_phase_with_forced_delay(
            session_factory_arg, run_id_arg, code, label
        ):
            if code == "queued":
                await asyncio.sleep(0.2)
            await original_record_phase(session_factory_arg, run_id_arg, code, label)

        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            patch(
                "openhands.automation.dispatcher._record_run_phase",
                side_effect=_record_phase_with_forced_delay,
            ),
        ):
            tasks_before = asyncio.all_tasks()
            dispatched = await dispatch_pending_runs(
                async_session_factory, mock_settings, mock_client
            )
            assert len(dispatched) == 1

            # Under a mis-ordered "queued" write, the spawned task can race
            # all the way to completion (and drop out of all_tasks()) before
            # dispatch_pending_runs even returns — so 0 new tasks found here
            # is possible and fine; await whatever is still in flight rather
            # than requiring exactly one.
            # Match by the name dispatch_pending_runs gives the task, so a
            # background task some future driver spawns here can never be
            # mistaken for ours and awaited forever.
            new_tasks = {
                t
                for t in asyncio.all_tasks() - tasks_before
                if (t.get_name() or "").startswith("execute-run-")
            }
            assert len(new_tasks) <= 1
            for task in new_tasks:
                await task  # run the real _execute_run_safe to completion

        # The three phases inside the spawned task never race anything else —
        # no other coroutine writes phase_code while it runs — so their
        # relative order is safe to assert directly from the hooks above.
        # "queued" is deliberately not read live here: by the time any
        # separate read could observe it, the spawned task may already have
        # raced past it (see docstring), so its ordering is proven instead by
        # the final-state assertion below plus the fully-controlled sequence
        # in test_phases_recorded_in_order_with_nondecreasing_timestamps.
        codes = [code for code, _, _ in hook_snapshots]
        assert codes == ["sandbox_provisioning", "bundle_upload", "entrypoint_start"]

        timestamps = [ts for _, _, ts in hook_snapshots]
        assert timestamps == sorted(timestamps)

        # The real proof for this test: even though "queued" is the last call
        # to actually *commit* (it was forced to lag behind), the run's final
        # observable phase must still be "entrypoint_start" — not "queued"
        # clobbering it after the fact. This is exactly what a "queued" write
        # reordered to after create_task would break.
        async with async_session_factory() as session:
            final = await session.get(AutomationRun, run_id)
            assert final.phase_code == "entrypoint_start"
            assert final.phase_label == "Starting entrypoint"
            assert final.bash_command_id == "cmd-1"
            assert final.phase_updated_at >= timestamps[-1]
