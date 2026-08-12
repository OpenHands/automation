"""Tests for local mode support in dispatcher and watchdog."""

import pytest

from openhands.automation.utils.agent_server import (
    BashCommandResult,
    VerificationResult,
    extract_conversation_id,
    get_conversation_task_outcome,
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


class TestTaskOutcomeRecoveryHelpers:
    """Tests for recovering task outcomes from agent-server conversations."""

    def test_extract_conversation_id_from_printed_id(self):
        conversation_id = "12345678-1234-5678-1234-567812345678"
        output = f"  conversation id: {conversation_id}\nALL_OK"

        assert extract_conversation_id(output) == conversation_id

    def test_extract_conversation_id_from_session_url(self):
        conversation_id = "12345678-1234-5678-1234-567812345678"
        output = (
            f"session URL: https://app.all-hands.dev/conversations/{conversation_id}"
        )

        assert extract_conversation_id(output) == conversation_id

    @pytest.mark.asyncio
    async def test_get_conversation_task_outcome(self):
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        outcome = {"status": "blocked", "summary": "Missing Slack MCP"}
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"task_outcome": outcome}
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_conversation_task_outcome(
            mock_client,
            "http://localhost:3000",
            "test-key",
            "12345678-1234-5678-1234-567812345678",
        )

        assert result == outcome
        mock_client.get.assert_awaited_once()
        _, kwargs = mock_client.get.call_args
        assert kwargs["headers"] == {"X-Session-API-Key": "test-key"}


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

    @pytest.mark.asyncio
    async def test_adds_command_id_filter_when_provided(self):
        """When command_id is provided, params include command_id__eq."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"items": []}
        mock_client.get = AsyncMock(return_value=mock_response)

        await get_last_bash_command_result(
            mock_client,
            "http://localhost:3000",
            "test-key",
            command_id="abc123",
        )

        # Verify the request was made with command_id__eq in params
        mock_client.get.assert_called_once()
        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["command_id__eq"] == "abc123"
        assert kwargs["params"]["kind__eq"] == "BashOutput"
        assert kwargs["params"]["sort_order"] == "TIMESTAMP_DESC"
        assert kwargs["params"]["limit"] == 1

    @pytest.mark.asyncio
    async def test_omits_command_id_filter_when_none(self):
        """When command_id is None, params do NOT include command_id__eq."""
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"items": []}
        mock_client.get = AsyncMock(return_value=mock_response)

        await get_last_bash_command_result(
            mock_client,
            "http://localhost:3000",
            "test-key",
        )

        mock_client.get.assert_called_once()
        _, kwargs = mock_client.get.call_args
        assert "command_id__eq" not in kwargs["params"]
        assert kwargs["params"]["kind__eq"] == "BashOutput"


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
    async def test_returns_recovered_task_outcome(self):
        """Verified runs include task outcome fetched from conversation info."""
        from unittest.mock import patch

        conversation_id = "12345678-1234-5678-1234-567812345678"
        outcome = {"status": "success", "summary": "Done"}
        mock_result = BashCommandResult(
            found=True,
            exit_code=0,
            stdout=f"conversation id: {conversation_id}\nDone",
            stderr="",
        )

        with (
            patch(
                "openhands.automation.utils.agent_server.get_last_bash_command_result"
            ) as mock_get,
            patch(
                "openhands.automation.utils.agent_server.get_conversation_task_outcome"
            ) as mock_outcome,
        ):
            mock_get.return_value = mock_result
            mock_outcome.return_value = outcome

            result = await verify_run_on_agent_server(
                agent_url="http://localhost:3000",
                session_key="test-key",
                run_id="run-123",
            )

        assert result.verified is True
        assert result.conversation_id == conversation_id
        assert result.task_outcome == outcome

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
