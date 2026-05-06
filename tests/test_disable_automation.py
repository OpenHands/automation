"""Tests for automatic disabling of automations with erroneous configurations.

When an automation has a permanent error (e.g., tarball URL doesn't exist),
the system should automatically disable it to prevent repeated failed runs.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from automation.exceptions import PermanentDispatchError, TarballNotFoundError
from automation.execution import _is_permanent_http_error


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
def mock_client():
    """Mock httpx.AsyncClient for tests."""
    return MagicMock()


def _create_mock_backend() -> MagicMock:
    """Create a mock backend for dispatcher tests."""
    from automation.backends.base import ExecutionContext

    mock_backend = MagicMock()
    mock_backend.get_api_key = AsyncMock(return_value="test-api-key")
    mock_backend.build_env_vars = MagicMock(return_value={})
    mock_backend.is_local_mode = False
    # Mock execution context methods
    mock_ctx = ExecutionContext(
        agent_url="http://localhost:3000",
        session_key="test-session-key",
        sandbox_id=None,
    )
    mock_backend.get_execution_context = AsyncMock(return_value=mock_ctx)
    mock_backend.release_context = AsyncMock(return_value=None)
    return mock_backend


def _docker_available() -> bool:
    """Check if Docker is available for testcontainers."""
    try:
        import socket

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect("/var/run/docker.sock")
        sock.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available for testcontainers",
)


class TestExceptions:
    """Tests for the custom exception hierarchy."""

    def test_tarball_not_found_is_permanent_error(self):
        """TarballNotFoundError is a PermanentDispatchError."""
        exc = TarballNotFoundError("test")
        assert isinstance(exc, PermanentDispatchError)

    def test_permanent_dispatch_error_is_exception(self):
        """PermanentDispatchError is a standard Exception."""
        exc = PermanentDispatchError("test")
        assert isinstance(exc, Exception)


class TestIsPermanentHttpError:
    """Tests for _is_permanent_http_error function."""

    def test_404_is_permanent(self):
        """HTTP 404 Not Found is a permanent error."""
        stderr = "curl: (22) The requested URL returned error: 404"
        assert _is_permanent_http_error(stderr) is True

    def test_403_is_permanent(self):
        """HTTP 403 Forbidden is a permanent error."""
        stderr = "curl: (22) The requested URL returned error: 403"
        assert _is_permanent_http_error(stderr) is True

    def test_401_is_permanent(self):
        """HTTP 401 Unauthorized is a permanent error."""
        stderr = "curl: (22) The requested URL returned error: 401"
        assert _is_permanent_http_error(stderr) is True

    def test_400_is_permanent(self):
        """HTTP 400 Bad Request is a permanent error."""
        stderr = "curl: (22) The requested URL returned error: 400"
        assert _is_permanent_http_error(stderr) is True

    def test_410_is_permanent(self):
        """HTTP 410 Gone is a permanent error."""
        stderr = "curl: (22) The requested URL returned error: 410"
        assert _is_permanent_http_error(stderr) is True

    def test_500_is_not_permanent(self):
        """HTTP 500 Internal Server Error is a transient error."""
        stderr = "curl: (22) The requested URL returned error: 500"
        assert _is_permanent_http_error(stderr) is False

    def test_502_is_not_permanent(self):
        """HTTP 502 Bad Gateway is a transient error."""
        stderr = "curl: (22) The requested URL returned error: 502"
        assert _is_permanent_http_error(stderr) is False

    def test_503_is_not_permanent(self):
        """HTTP 503 Service Unavailable is a transient error."""
        stderr = "curl: (22) The requested URL returned error: 503"
        assert _is_permanent_http_error(stderr) is False

    def test_no_status_code_is_not_permanent(self):
        """Non-HTTP errors are not permanent."""
        stderr = "curl: (7) Failed to connect to host.example.com"
        assert _is_permanent_http_error(stderr) is False

    def test_empty_stderr_is_not_permanent(self):
        """Empty stderr is not permanent."""
        assert _is_permanent_http_error("") is False

    def test_timeout_error_is_not_permanent(self):
        """Timeout errors are not permanent."""
        stderr = "curl: (28) Operation timed out after 30000 milliseconds"
        assert _is_permanent_http_error(stderr) is False


@requires_docker
class TestDisableAutomation:
    """Tests for the disable_automation function."""

    async def test_disables_enabled_automation(self, async_session_factory):
        """An enabled automation is disabled and returns True."""
        from automation.models import Automation
        from automation.utils.run import disable_automation

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="https://example.com/missing.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        result = await disable_automation(
            async_session_factory, automation_id, "Tarball not found"
        )

        assert result is True

        async with async_session_factory() as session:
            db_result = await session.execute(
                select(Automation).where(Automation.id == automation_id)
            )
            automation = db_result.scalars().first()
            assert automation.enabled is False

    async def test_returns_false_for_already_disabled(self, async_session_factory):
        """Already disabled automation returns False."""
        from automation.models import Automation
        from automation.utils.run import disable_automation

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="https://example.com/missing.tar.gz",
                entrypoint="uv run main.py",
                enabled=False,  # Already disabled
            )
            session.add(automation)
            await session.commit()
            automation_id = automation.id

        result = await disable_automation(
            async_session_factory, automation_id, "Tarball not found"
        )

        assert result is False

    async def test_returns_false_for_nonexistent(self, async_session_factory):
        """Non-existent automation returns False."""
        from automation.utils.run import disable_automation

        fake_id = uuid.uuid4()
        result = await disable_automation(
            async_session_factory, fake_id, "Tarball not found"
        )

        assert result is False


@requires_docker
class TestDownloadInternalTarball:
    """Tests for _download_internal_tarball raising TarballNotFoundError."""

    async def test_raises_tarball_not_found_for_missing_upload(
        self, async_session_factory
    ):
        """TarballNotFoundError is raised when upload record doesn't exist."""
        from automation.dispatcher import _download_internal_tarball

        fake_upload_id = uuid.uuid4()

        async with async_session_factory() as session:
            with pytest.raises(TarballNotFoundError) as exc_info:
                await _download_internal_tarball(fake_upload_id, session)

        assert "not found" in str(exc_info.value).lower()
        assert str(fake_upload_id) in str(exc_info.value)


@requires_docker
class TestExecuteRunDisablesAutomation:
    """Tests that _execute_run disables automation on permanent errors."""

    @patch("automation.dispatcher.execute_in_context")
    @patch("automation.dispatcher.get_backend")
    async def test_disables_automation_on_internal_tarball_not_found(
        self,
        mock_get_backend,
        mock_execute,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """Automation is disabled when internal tarball upload is not found."""
        from automation.dispatcher import _execute_run
        from automation.models import Automation, AutomationRun, AutomationRunStatus

        mock_get_backend.return_value = _create_mock_backend()

        # Create an automation with a non-existent internal tarball
        fake_upload_id = uuid.uuid4()
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path=f"oh-internal://uploads/{fake_upload_id}",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
            )
            session.add(run)
            await session.commit()
            automation_id = automation.id

        # Re-fetch with automation relationship loaded
        async with async_session_factory() as session:
            from sqlalchemy.orm import selectinload

            result = await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.automation_id == automation_id)
            )
            run = result.scalars().first()

            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        # Verify automation was disabled
        async with async_session_factory() as session:
            result = await session.execute(
                select(Automation).where(Automation.id == automation_id)
            )
            automation = result.scalars().first()
            assert automation.enabled is False

        # Verify run was marked as FAILED
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(
                    AutomationRun.automation_id == automation_id
                )
            )
            run = result.scalars().first()
            assert run.status == AutomationRunStatus.FAILED
            assert "not found" in run.error_detail.lower()

    @patch("automation.dispatcher.execute_in_context")
    @patch("automation.dispatcher.get_backend")
    async def test_does_not_disable_on_transient_error(
        self,
        mock_get_backend,
        mock_execute,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """Automation is NOT disabled on transient errors like network failures."""
        from automation.dispatcher import _execute_run
        from automation.execution import DispatchResult
        from automation.models import Automation, AutomationRun, AutomationRunStatus

        mock_get_backend.return_value = _create_mock_backend()
        # Simulate a transient execution failure
        mock_execute.return_value = DispatchResult(
            success=False, sandbox_id=None, error="Connection timeout"
        )

        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Test Automation",
                trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                tarball_path="https://example.com/valid.tar.gz",
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()

            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
            )
            session.add(run)
            await session.commit()
            automation_id = automation.id

        # Re-fetch with automation relationship loaded
        async with async_session_factory() as session:
            from sqlalchemy.orm import selectinload

            result = await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.automation_id == automation_id)
            )
            run = result.scalars().first()

            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        # Verify automation is still enabled (transient error)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Automation).where(Automation.id == automation_id)
            )
            automation = result.scalars().first()
            assert automation.enabled is True

        # Verify run was marked as FAILED
        async with async_session_factory() as session:
            result = await session.execute(
                select(AutomationRun).where(
                    AutomationRun.automation_id == automation_id
                )
            )
            run = result.scalars().first()
            assert run.status == AutomationRunStatus.FAILED
