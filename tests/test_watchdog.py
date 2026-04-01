"""Tests for the watchdog module.

The watchdog processes stale runs (RUNNING but past timeout_at) and marks them
with appropriate status based on sandbox verification results.

Also tests delayed sandbox cleanup functionality.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from automation.models import Automation, AutomationRun, AutomationRunStatus
from automation.utils import utcnow
from automation.utils.sandbox import VerificationResult
from automation.watchdog import (
    _compute_cleanup_at,
    _verify_and_mark_run,
    cleanup_pending_sandboxes,
)


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
async def automation_with_run(async_session_factory):
    """Create an automation with a RUNNING run that is past timeout."""
    async with async_session_factory() as session:
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
            timeout=60,
        )
        session.add(automation)
        await session.commit()

        now = utcnow()
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            sandbox_id="test-sandbox-123",
            started_at=now - timedelta(minutes=5),
            timeout_at=now - timedelta(minutes=1),  # Already past timeout
        )
        session.add(run)
        await session.commit()

        yield {"automation": automation, "run": run, "run_id": run.id}


class TestVerifyAndMarkRunExitCodes:
    """Tests for _verify_and_mark_run handling different exit codes."""

    @pytest.mark.asyncio
    async def test_exit_code_0_marks_completed(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code 0 means command succeeded - mark as COMPLETED."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=True,
            exit_code=0,
            stdout="Success output",
            stderr="",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True

        # Verify the run was marked as COMPLETED
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.COMPLETED
            assert run.completed_at is not None
            assert run.error_detail is None

    @pytest.mark.asyncio
    async def test_exit_code_minus_1_marks_timed_out(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code -1 means command was killed/timed out."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=-1,
            stdout="",
            stderr="Command timed out after 60 seconds",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True

        # Verify the run was marked as FAILED with timeout message
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail
            assert "timed out" in run.error_detail.lower()

    @pytest.mark.asyncio
    async def test_exit_code_none_marks_timed_out(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code None means command was killed - mark as FAILED with timeout."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=None,
            stdout="",
            stderr="",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True

        # Verify the run was marked as FAILED with timeout message
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_marks_failed_without_timeout(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Non-zero exit code (not -1) means command failed."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=1,
            stdout="Some output",
            stderr="Error: something went wrong",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True

        # Verify the run was marked as FAILED with exit code (not timeout)
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "exit_code=1" in run.error_detail
            assert "Timed out" not in run.error_detail
            assert "stderr: Error: something went wrong" in run.error_detail

    @pytest.mark.asyncio
    async def test_exit_code_127_marks_failed_without_timeout(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """Exit code 127 (command not found) - mark as FAILED without timeout."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=True,
            success=False,
            exit_code=127,
            stdout="",
            stderr="bash: command not found",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True

        # Verify the run was marked as FAILED with exit code (not timeout)
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert "exit_code=127" in run.error_detail
            assert "Timed out" not in run.error_detail


class TestVerifyAndMarkRunVerificationFailed:
    """Tests for _verify_and_mark_run when verification fails."""

    @pytest.mark.asyncio
    async def test_verification_failed_marks_timed_out(
        self, async_session_factory, automation_with_run, mock_settings
    ):
        """When verification fails (sandbox unavailable), mark as timed out."""
        run_id = automation_with_run["run_id"]

        verification = VerificationResult(
            verified=False,
            error="Sandbox not available",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_cleanup.assert_called_once()

        # Verify the run was marked as FAILED with timeout message
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.FAILED
            assert run.completed_at is not None
            assert "Timed out" in run.error_detail
            assert "Sandbox not available" in run.error_detail

    @pytest.mark.asyncio
    async def test_verification_failed_no_cleanup_if_keep_alive(
        self, async_session_factory, mock_settings
    ):
        """When keep_alive is True, don't cleanup sandbox on verification failure."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Keep Alive Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                sandbox_id="test-sandbox-456",
                started_at=now - timedelta(minutes=5),
                timeout_at=now - timedelta(minutes=1),
                keep_alive=True,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        verification = VerificationResult(
            verified=False,
            error="Sandbox not available",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        # Cleanup should NOT be called when keep_alive is True
        mock_cleanup.assert_not_called()


class TestComputeCleanupAt:
    """Tests for _compute_cleanup_at helper function."""

    def test_returns_none_when_delay_is_zero(self, mock_settings):
        """When delay is 0, should return None (immediate cleanup)."""
        assert mock_settings.sandbox_cleanup_delay_mins == 0
        result = _compute_cleanup_at(mock_settings)
        assert result is None

    def test_returns_future_timestamp_when_delay_positive(
        self, mock_settings_delayed_cleanup
    ):
        """When delay > 0, should return now + delay."""
        assert mock_settings_delayed_cleanup.sandbox_cleanup_delay_mins == 60
        now = utcnow()
        result = _compute_cleanup_at(mock_settings_delayed_cleanup, now=now)
        expected = now + timedelta(minutes=60)
        assert result == expected

    def test_uses_current_time_when_now_not_provided(
        self, mock_settings_delayed_cleanup
    ):
        """Should use current time when now parameter is not provided."""
        before = utcnow()
        result = _compute_cleanup_at(mock_settings_delayed_cleanup)
        after = utcnow()

        # Result should be set (delay > 0) and roughly 60 minutes in the future
        assert result is not None
        expected_min = before + timedelta(minutes=60)
        expected_max = after + timedelta(minutes=60)
        assert expected_min <= result <= expected_max


class TestDelayedCleanup:
    """Tests for delayed sandbox cleanup behavior."""

    @pytest.mark.asyncio
    async def test_delayed_cleanup_sets_cleanup_at(
        self, async_session_factory, mock_settings_delayed_cleanup
    ):
        """When delay > 0, should set cleanup_at instead of immediate cleanup."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Delayed Cleanup Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                sandbox_id="test-sandbox-delayed",
                started_at=now - timedelta(minutes=5),
                timeout_at=now - timedelta(minutes=1),
                keep_alive=False,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        verification = VerificationResult(
            verified=True,
            success=True,
            exit_code=0,
            stdout="Success",
            stderr="",
        )

        with (
            patch(
                "automation.watchdog.verify_run_status",
                new_callable=AsyncMock,
                return_value=verification,
            ),
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(
                    session, run, mock_settings_delayed_cleanup
                )
                await session.commit()

        assert result is True
        # Cleanup should NOT be called immediately when delay > 0
        mock_cleanup.assert_not_called()

        # Verify cleanup_at was set
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.status == AutomationRunStatus.COMPLETED
            assert run.cleanup_at is not None
            # cleanup_at should be ~60 minutes after completed_at
            delta = run.cleanup_at - run.completed_at
            assert timedelta(minutes=59) < delta < timedelta(minutes=61)


class TestCleanupPendingSandboxes:
    """Tests for the cleanup_pending_sandboxes function."""

    @pytest.mark.asyncio
    async def test_cleans_up_runs_past_cleanup_at(
        self, async_session_factory, mock_settings
    ):
        """Should clean up runs where cleanup_at has passed."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Cleanup Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            # Run with cleanup_at in the past
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                sandbox_id="test-sandbox-to-cleanup",
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1),
                cleanup_at=now - timedelta(minutes=1),  # Past deadline
                keep_alive=False,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_cleanup,
        ):
            cleaned = await cleanup_pending_sandboxes(
                async_session_factory, mock_settings
            )

        assert cleaned == 1
        mock_cleanup.assert_called_once()

        # Verify sandbox_id and cleanup_at were cleared
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.sandbox_id is None
            assert run.cleanup_at is None

    @pytest.mark.asyncio
    async def test_skips_runs_not_past_cleanup_at(
        self, async_session_factory, mock_settings
    ):
        """Should not clean up runs where cleanup_at is in the future."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Future Cleanup Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            # Run with cleanup_at in the future
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                sandbox_id="test-sandbox-not-yet",
                started_at=now - timedelta(hours=1),
                completed_at=now - timedelta(minutes=30),
                cleanup_at=now + timedelta(minutes=30),  # Future deadline
                keep_alive=False,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "automation.watchdog.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="test-api-key",
            ),
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            cleaned = await cleanup_pending_sandboxes(
                async_session_factory, mock_settings
            )

        assert cleaned == 0
        mock_cleanup.assert_not_called()

        # sandbox_id should still be set
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.sandbox_id == "test-sandbox-not-yet"
            assert run.cleanup_at is not None

    @pytest.mark.asyncio
    async def test_skips_runs_with_keep_alive(
        self, async_session_factory, mock_settings
    ):
        """Should not clean up runs where keep_alive is True."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Keep Alive Cleanup Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            # Run with cleanup_at in the past but keep_alive=True
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                sandbox_id="test-sandbox-keep-alive",
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1),
                cleanup_at=now - timedelta(minutes=1),  # Past deadline
                keep_alive=True,  # Should prevent cleanup
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            cleaned = await cleanup_pending_sandboxes(
                async_session_factory, mock_settings
            )

        assert cleaned == 0
        mock_cleanup.assert_not_called()

        # sandbox_id should still be set
        async with async_session_factory() as session:
            run = await session.get(AutomationRun, run_id)
            assert run.sandbox_id == "test-sandbox-keep-alive"

    @pytest.mark.asyncio
    async def test_skips_runs_without_sandbox_id(
        self, async_session_factory, mock_settings
    ):
        """Should not process runs where sandbox_id is already cleared."""
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="No Sandbox Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            now = utcnow()
            # Run with cleanup_at in the past but no sandbox_id
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.COMPLETED,
                sandbox_id=None,  # Already cleaned up
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1),
                cleanup_at=now - timedelta(minutes=1),
                keep_alive=False,
            )
            session.add(run)
            await session.commit()

        with (
            patch(
                "automation.watchdog.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            cleaned = await cleanup_pending_sandboxes(
                async_session_factory, mock_settings
            )

        assert cleaned == 0
        mock_cleanup.assert_not_called()
