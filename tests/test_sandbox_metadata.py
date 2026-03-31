"""Tests for the sandbox_metadata utility module."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from automation.utils.sandbox_metadata import set_sandbox_automation_metadata


class TestSetSandboxAutomationMetadata:
    """Tests for set_sandbox_automation_metadata function."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_service_key(self):
        """Should return False and skip API call when service_key is empty."""
        result = await set_sandbox_automation_metadata(
            api_url="https://test.example.com",
            service_key="",
            sandbox_id="sandbox-123",
            automation_id="auto-456",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_service_key_is_none(self):
        """Should return False and skip API call when service_key is None."""
        result = await set_sandbox_automation_metadata(
            api_url="https://test.example.com",
            service_key=None,  # type: ignore
            sandbox_id="sandbox-123",
            automation_id="auto-456",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_successful_api_call_returns_true(self):
        """Should return True when API call succeeds."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await set_sandbox_automation_metadata(
                api_url="https://test.example.com",
                service_key="test-service-key",
                sandbox_id="sandbox-123",
                automation_id="auto-456",
                automation_name="Test Automation",
                trigger_type="cron",
                run_id="run-789",
            )

            assert result is True
            mock_client.put.assert_called_once()
            call_args = mock_client.put.call_args
            assert call_args[0][0] == "https://test.example.com/api/service/sandboxes/sandbox-123/automation-metadata"
            assert call_args[1]["headers"]["X-Service-API-Key"] == "test-service-key"
            assert call_args[1]["json"]["automation_id"] == "auto-456"
            assert call_args[1]["json"]["automation_name"] == "Test Automation"
            assert call_args[1]["json"]["trigger_type"] == "cron"
            assert call_args[1]["json"]["run_id"] == "run-789"

    @pytest.mark.asyncio
    async def test_api_url_trailing_slash_is_stripped(self):
        """Should strip trailing slash from api_url."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await set_sandbox_automation_metadata(
                api_url="https://test.example.com/",  # Note trailing slash
                service_key="test-key",
                sandbox_id="sandbox-123",
            )

            call_args = mock_client.put.call_args
            # Should not have double slash
            assert "//" not in call_args[0][0].replace("https://", "")

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self):
        """Should return False and log warning on HTTP error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=mock_response,
            )
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await set_sandbox_automation_metadata(
                api_url="https://test.example.com",
                service_key="test-key",
                sandbox_id="sandbox-123",
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_connection_error_returns_false(self):
        """Should return False and log warning on connection error."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await set_sandbox_automation_metadata(
                api_url="https://test.example.com",
                service_key="test-key",
                sandbox_id="sandbox-123",
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_timeout_error_returns_false(self):
        """Should return False and log warning on timeout."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(
                side_effect=httpx.TimeoutException("Request timed out")
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await set_sandbox_automation_metadata(
                api_url="https://test.example.com",
                service_key="test-key",
                sandbox_id="sandbox-123",
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_extra_metadata_is_included_in_payload(self):
        """Should include extra_metadata in the request payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            extra = {"custom_key": "custom_value", "number": 42}
            await set_sandbox_automation_metadata(
                api_url="https://test.example.com",
                service_key="test-key",
                sandbox_id="sandbox-123",
                extra_metadata=extra,
            )

            call_args = mock_client.put.call_args
            assert call_args[1]["json"]["extra_metadata"] == extra

    @pytest.mark.asyncio
    async def test_none_values_are_passed_to_api(self):
        """Should pass None values to API (they are valid)."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await set_sandbox_automation_metadata(
                api_url="https://test.example.com",
                service_key="test-key",
                sandbox_id="sandbox-123",
                # All optional params are None by default
            )

            call_args = mock_client.put.call_args
            json_payload = call_args[1]["json"]
            assert json_payload["automation_id"] is None
            assert json_payload["automation_name"] is None
            assert json_payload["trigger_type"] is None
            assert json_payload["run_id"] is None
            # extra_metadata should not be in payload when None
            assert "extra_metadata" not in json_payload
