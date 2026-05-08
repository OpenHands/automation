"""Tests for the watchdog module.

The watchdog processes stale runs (RUNNING but past timeout_at) and marks them
with appropriate status based on sandbox verification results.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils import utcnow
from openhands.automation.utils.agent_server import VerificationResult
from openhands.automation.watchdog import _verify_and_mark_run


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


def _create_mock_backend(verification_result: VerificationResult) -> MagicMock:
    """Create a mock backend with configured verification result."""
    mock_backend = MagicMock()
    mock_backend.verify_run = AsyncMock(return_value=verification_result)
    mock_backend.cleanup_after_verification = AsyncMock()
    mock_backend.get_api_key = AsyncMock(return_value="test-api-key")
    return mock_backend


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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        mock_backend.cleanup_after_verification.assert_called_once()

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

        mock_backend = _create_mock_backend(verification)
        with patch(
            "openhands.automation.watchdog.get_backend", return_value=mock_backend
        ):
            async with async_session_factory() as session:
                run = await session.get(AutomationRun, run_id)
                result = await _verify_and_mark_run(session, run, mock_settings)
                await session.commit()

        assert result is True
        # Cleanup should NOT be called when keep_alive is True
        mock_backend.cleanup_after_verification.assert_not_called()
