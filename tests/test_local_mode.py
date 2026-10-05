"""Tests for local mode support in dispatcher and watchdog."""

import pytest

from openhands.automation.utils.agent_server import (
    BashCommandResult,
    VerificationOutcome,
    VerificationResult,
    get_last_bash_command_result,
    verify_run_on_agent_server,
)


class TestBashCommandResult:
    """Tests for BashCommandResult dataclass."""

    def test_not_found(self):
        """BashCommandResult with found=False."""
        result = BashCommandResult(found=False, error="No bash output found")
        assert result.found is False
        assert result.exit_code is None
        assert result.error == "No bash output found"

    def test_still_running(self):
        """BashCommandResult when command is still running."""
        result = BashCommandResult(
            found=True, exit_code=None, error="Command still running"
        )
        assert result.found is True
        assert result.exit_code is None

    def test_completed_success(self):
        """BashCommandResult when command completed successfully."""
        result = BashCommandResult(
            found=True,
            exit_code=0,
            stdout="Hello, world!",
            stderr="",
        )
        assert result.found is True
        assert result.exit_code == 0
        assert result.stdout == "Hello, world!"

    def test_completed_failure(self):
        """BashCommandResult when command failed."""
        result = BashCommandResult(
            found=True,
            exit_code=1,
            stdout="",
            stderr="Error: file not found",
        )
        assert result.found is True
        assert result.exit_code == 1
        assert result.stderr == "Error: file not found"


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_not_verified(self):
        """VerificationResult when verification failed."""
        result = VerificationResult(verified=False, error="Sandbox not available")
        assert result.outcome == VerificationOutcome.ENVIRONMENT_UNAVAILABLE
        assert result.verified is False
        assert result.success is None
        assert result.error == "Sandbox not available"

    def test_verified_success(self):
        """VerificationResult when run completed successfully."""
        result = VerificationResult(
            verified=True,
            success=True,
            exit_code=0,
            stdout="Done",
        )
        assert result.verified is True
        assert result.success is True
        assert result.exit_code == 0

    def test_verified_failure(self):
        """VerificationResult when run failed."""
        result = VerificationResult(
            verified=True,
            success=False,
            exit_code=1,
            stderr="Error",
        )
        assert result.verified is True
        assert result.success is False
        assert result.exit_code == 1


class TestGetLastBashCommandResult:
    """Tests for get_last_bash_command_result function."""

    @staticmethod
    def _workspace(output=None, error=None):
        from unittest.mock import AsyncMock, MagicMock

        workspace = MagicMock()
        workspace.__aenter__ = AsyncMock(return_value=workspace)
        workspace.__aexit__ = AsyncMock(return_value=None)
        workspace.get_command_output = AsyncMock(return_value=output)
        if error is not None:
            workspace.get_command_output.side_effect = error
        return workspace

    @pytest.mark.asyncio
    async def test_handles_http_error(self):
        """Returns error result when HTTP request fails."""
        from unittest.mock import MagicMock, patch

        import httpx

        error = httpx.HTTPStatusError(
            "Not found", request=MagicMock(), response=MagicMock(status_code=404)
        )
        workspace = self._workspace(error=error)
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            result = await get_last_bash_command_result(
                "http://localhost:3000", "test-key"
            )

        assert result.found is False
        assert result.error is not None and "Not found" in result.error

    @pytest.mark.asyncio
    async def test_handles_transient_rate_limit(self):
        """Returns structured transient info for retryable agent-server errors."""
        from unittest.mock import MagicMock, patch

        import httpx

        error = httpx.HTTPStatusError(
            "Rate limited", request=MagicMock(), response=MagicMock(status_code=429)
        )
        workspace = self._workspace(error=error)
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            result = await get_last_bash_command_result(
                "http://localhost:3000", "test-key"
            )

        assert result.found is False
        assert result.error is not None and "HTTP 429" in result.error
        assert result.error_info is not None
        assert (
            result.error_info.fingerprint
            == "agent_server:bash_events_search:rate_limited:429"
        )

    @pytest.mark.asyncio
    async def test_handles_empty_response(self):
        """Returns error result when no bash output found."""
        from unittest.mock import patch

        workspace = self._workspace(output=None)
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            result = await get_last_bash_command_result(
                "http://localhost:3000", "test-key"
            )

        assert result.found is False
        assert result.error == "No bash output found"

    @pytest.mark.asyncio
    async def test_handles_running_command(self):
        """Returns running result when exit_code is None."""
        from unittest.mock import patch

        workspace = self._workspace(
            output={"exit_code": None, "stdout": "", "stderr": ""}
        )
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            result = await get_last_bash_command_result(
                "http://localhost:3000", "test-key"
            )

        assert result.found is True
        assert result.exit_code is None
        assert result.error == "Command still running"

    @pytest.mark.asyncio
    async def test_handles_completed_command(self):
        """Returns completed result with exit code and output."""
        from unittest.mock import patch

        workspace = self._workspace(
            output={"exit_code": 0, "stdout": "Hello", "stderr": ""}
        )
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            result = await get_last_bash_command_result(
                "http://localhost:3000", "test-key"
            )

        assert result.found is True
        assert result.exit_code == 0
        assert result.stdout == "Hello"

    @pytest.mark.asyncio
    async def test_passes_command_id_to_sdk_when_provided(self):
        """The SDK receives the command ID when one is available."""
        from unittest.mock import patch

        workspace = self._workspace(output=None)
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            await get_last_bash_command_result(
                "http://localhost:3000",
                "test-key",
                command_id="abc123",
            )

        workspace.get_command_output.assert_awaited_once_with("abc123")

    @pytest.mark.asyncio
    async def test_passes_none_to_sdk_without_command_id(self):
        """The SDK receives None when no command ID is available."""
        from unittest.mock import patch

        workspace = self._workspace(output=None)
        with patch(
            "openhands.automation.utils.agent_server.AsyncRemoteWorkspace",
            return_value=workspace,
        ):
            await get_last_bash_command_result(
                "http://localhost:3000",
                "test-key",
            )

        workspace.get_command_output.assert_awaited_once_with(None)


class TestVerifyRunOnAgentServer:
    """Tests for verify_run_on_agent_server function."""

    @pytest.mark.asyncio
    async def test_returns_not_verified_on_http_error(self):
        """Returns not verified when HTTP client fails."""
        from unittest.mock import patch

        mock_result = BashCommandResult(found=False, error="Connection refused")

        with patch(
            "openhands.automation.utils.agent_server.get_last_bash_command_result"
        ) as mock_get:
            mock_get.return_value = mock_result

            result = await verify_run_on_agent_server(
                agent_url="http://localhost:3000",
                session_key="test-key",
                run_id="run-123",
            )

        assert result.verified is False
        assert result.error == "Connection refused"

    @pytest.mark.asyncio
    async def test_returns_not_verified_when_still_running(self):
        """Returns not verified when command still running."""
        from unittest.mock import patch

        mock_result = BashCommandResult(
            found=True, exit_code=None, error="Command still running"
        )

        with patch(
            "openhands.automation.utils.agent_server.get_last_bash_command_result"
        ) as mock_get:
            mock_get.return_value = mock_result

            result = await verify_run_on_agent_server(
                agent_url="http://localhost:3000",
                session_key="test-key",
                run_id="run-123",
            )

        assert result.verified is False
        assert result.error == "Command still running"

    @pytest.mark.asyncio
    async def test_returns_verified_success(self):
        """Returns verified success when exit_code is 0."""
        from unittest.mock import patch

        mock_result = BashCommandResult(
            found=True, exit_code=0, stdout="Done", stderr=""
        )

        with patch(
            "openhands.automation.utils.agent_server.get_last_bash_command_result"
        ) as mock_get:
            mock_get.return_value = mock_result

            result = await verify_run_on_agent_server(
                agent_url="http://localhost:3000",
                session_key="test-key",
                run_id="run-123",
            )

        assert result.verified is True
        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout == "Done"

    @pytest.mark.asyncio
    async def test_returns_verified_failure(self):
        """Returns verified failure when exit_code is non-zero."""
        from unittest.mock import patch

        mock_result = BashCommandResult(
            found=True, exit_code=1, stdout="", stderr="Error"
        )

        with patch(
            "openhands.automation.utils.agent_server.get_last_bash_command_result"
        ) as mock_get:
            mock_get.return_value = mock_result

            result = await verify_run_on_agent_server(
                agent_url="http://localhost:3000",
                session_key="test-key",
                run_id="run-123",
            )

        assert result.verified is True
        assert result.success is False
        assert result.exit_code == 1
        assert result.stderr == "Error"


class TestDispatcherLocalMode:
    """Tests for dispatcher local mode behavior."""

    def test_local_mode_env_vars(self, monkeypatch):
        """Verify local mode injects correct env vars."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "local-key")

        from openhands.automation.config import clear_config_cache, get_config

        clear_config_cache()
        config = get_config()
        settings = config.service

        assert settings.is_local_mode is True
        assert settings.agent_server_url == "http://localhost:3000"
        assert settings.agent_server_api_key == "local-key"

    def test_cloud_mode_default(self, monkeypatch):
        """Verify cloud mode is default when agent_server_url not set."""
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_API_KEY", raising=False)

        from openhands.automation.config import clear_config_cache, get_config

        clear_config_cache()
        config = get_config()
        settings = config.service

        assert settings.is_local_mode is False


class TestWatchdogLocalMode:
    """Tests for watchdog local mode behavior."""

    def test_watchdog_uses_backend_abstraction(self):
        """Verify watchdog uses backend abstraction for verification."""
        from openhands.automation.watchdog import get_backend

        # Watchdog imports get_backend to delegate mode-specific logic
        assert callable(get_backend)

    def test_config_local_mode_property(self, monkeypatch):
        """Verify Settings has is_local_mode property."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")

        from openhands.automation.config import clear_config_cache, get_config

        clear_config_cache()
        settings = get_config().service

        assert hasattr(settings, "is_local_mode")
        assert settings.is_local_mode is True
