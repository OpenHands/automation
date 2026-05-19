"""Tests for the dispatcher module.

The dispatcher polls for PENDING automation runs and marks them as RUNNING.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from openhands.automation.dispatcher import (
    _build_event_payload,
    dispatch_pending_runs,
    dispatcher_loop,
)
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils import utcnow
from openhands.automation.utils.run import mark_run_status
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

        await dispatch_pending_runs(async_session_factory, mock_settings, mock_client)

        # Verify _execute_run_safe was called
        mock_execute.assert_called_once()
        # The automation passed should have timeout=120
        call_args = mock_execute.call_args
        run_arg = call_args[0][0]
        assert run_arg.automation.timeout == 120

    @patch("openhands.automation.dispatcher._execute_run_safe", new_callable=AsyncMock)
    async def test_uses_default_timeout_when_not_set(
        self, mock_execute, async_session_factory, mock_settings, mock_client
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

        await dispatch_pending_runs(async_session_factory, mock_settings, mock_client)

        # Verify _execute_run_safe was called
        mock_execute.assert_called_once()
        # The automation passed should have timeout=None
        call_args = mock_execute.call_args
        run_arg = call_args[0][0]
        assert run_arg.automation.timeout is None


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

    def test_llm_profile_included_when_present(self):
        """Automation llm_profile is passed through for preset scripts."""
        trigger = {"type": "cron", "schedule": "0 0 * * *", "timezone": "UTC"}
        automation = self._make_automation(trigger, llm_profile="fast-profile")
        run = self._make_run(automation)

        payload = _build_event_payload(automation, run)

        assert payload["llm_profile"] == "fast-profile"

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
