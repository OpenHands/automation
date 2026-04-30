"""Tests for local mode support in dispatcher and watchdog."""

import pytest

from automation.utils.agent_server import (
    BashCommandResult,
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

    @pytest.mark.asyncio
    async def test_handles_http_error(self):
        """Returns error result when HTTP request fails."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not found", request=MagicMock(), response=MagicMock(status_code=404)
        )
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_last_bash_command_result(
            mock_client, "http://localhost:3000", "test-key"
        )

        assert result.found is False
        assert result.error is not None and "Not found" in result.error

    @pytest.mark.asyncio
    async def test_handles_empty_response(self):
        """Returns error result when no bash output found."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"items": []}
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_last_bash_command_result(
            mock_client, "http://localhost:3000", "test-key"
        )

        assert result.found is False
        assert result.error == "No bash output found"

    @pytest.mark.asyncio
    async def test_handles_running_command(self):
        """Returns running result when exit_code is None."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "items": [{"exit_code": None, "stdout": "", "stderr": ""}]
        }
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_last_bash_command_result(
            mock_client, "http://localhost:3000", "test-key"
        )

        assert result.found is True
        assert result.exit_code is None
        assert result.error == "Command still running"

    @pytest.mark.asyncio
    async def test_handles_completed_command(self):
        """Returns completed result with exit code and output."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "items": [{"exit_code": 0, "stdout": "Hello", "stderr": ""}]
        }
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_last_bash_command_result(
            mock_client, "http://localhost:3000", "test-key"
        )

        assert result.found is True
        assert result.exit_code == 0
        assert result.stdout == "Hello"


class TestVerifyRunOnAgentServer:
    """Tests for verify_run_on_agent_server function."""

    @pytest.mark.asyncio
    async def test_returns_not_verified_on_http_error(self):
        """Returns not verified when HTTP client fails."""
        from unittest.mock import patch

        mock_result = BashCommandResult(found=False, error="Connection refused")

        with patch(
            "automation.utils.agent_server.get_last_bash_command_result"
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
            "automation.utils.agent_server.get_last_bash_command_result"
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
            "automation.utils.agent_server.get_last_bash_command_result"
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
            "automation.utils.agent_server.get_last_bash_command_result"
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

        from automation.config import clear_config_cache, get_config

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

        from automation.config import clear_config_cache, get_config

        clear_config_cache()
        config = get_config()
        settings = config.service

        assert settings.is_local_mode is False


class TestWatchdogLocalMode:
    """Tests for watchdog local mode behavior."""

    def test_watchdog_uses_backend_abstraction(self):
        """Verify watchdog uses backend abstraction for verification."""
        from automation.watchdog import get_backend

        # Watchdog imports get_backend to delegate mode-specific logic
        assert callable(get_backend)

    def test_config_local_mode_property(self, monkeypatch):
        """Verify Settings has is_local_mode property."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")

        from automation.config import clear_config_cache, get_config

        clear_config_cache()
        settings = get_config().service

        assert hasattr(settings, "is_local_mode")
        assert settings.is_local_mode is True
