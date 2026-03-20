"""Tests for authentication module."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from automation.app import app
from automation.auth import (
    AuthenticatedUser,
    _make_auth_request_with_retry,
    authenticate_request,
)
from automation.db import get_session


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request."""
    request = MagicMock()
    return request


@pytest.fixture
def mock_http_client():
    """Create a mock httpx client."""
    client = AsyncMock()
    client.is_closed = False
    return client


class TestAuthentication:
    """Tests for authenticate_request function.

    These tests call authenticate_request directly with injected dependencies,
    bypassing FastAPI's DI system for unit testing.
    """

    async def test_authenticate_valid_api_key(self, mock_request, mock_http_client):
        """Valid API key returns AuthenticatedUser with correct user_id and org_id."""
        mock_request.headers.get.return_value = "Bearer valid-api-key"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": 123,
            "name": "My API Key",
            "org_id": str(TEST_ORG_ID),
            "user_id": str(TEST_USER_ID),
            "auth_type": "bearer",
        }
        mock_http_client.get = AsyncMock(return_value=mock_response)

        result = await authenticate_request(mock_request, client=mock_http_client)

        assert isinstance(result, AuthenticatedUser)
        assert result.user_id == TEST_USER_ID
        assert result.org_id == TEST_ORG_ID
        assert result.api_key == "valid-api-key"

    async def test_authenticate_missing_header(self, mock_request, mock_http_client):
        """Missing Authorization header raises 401."""
        mock_request.headers.get.return_value = ""

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401
        assert "Missing or invalid Authorization header" in exc_info.value.detail

    async def test_authenticate_invalid_bearer_format(
        self, mock_request, mock_http_client
    ):
        """Invalid Bearer format raises 401."""
        mock_request.headers.get.return_value = "InvalidFormat token"

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401

    async def test_authenticate_invalid_key(self, mock_request, mock_http_client):
        """Invalid API key (401 from OpenHands) raises 401."""
        mock_request.headers.get.return_value = "Bearer invalid-key"

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_http_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401
        assert "Invalid or expired API key" in exc_info.value.detail

    async def test_authenticate_openhands_unavailable(
        self, mock_request, mock_http_client
    ):
        """Connection error to OpenHands API raises 502."""
        mock_request.headers.get.return_value = "Bearer valid-key"
        mock_http_client.get = AsyncMock(
            side_effect=httpx.RequestError("Connection failed")
        )

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 502
        assert "Failed to validate API key" in exc_info.value.detail

    async def test_authenticate_unexpected_status(self, mock_request, mock_http_client):
        """Unexpected status code from OpenHands API raises 502."""
        mock_request.headers.get.return_value = "Bearer valid-key"

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_http_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 502


class TestAuthIntegration:
    """Integration tests that exercise auth through actual API endpoints.

    These tests do NOT override the authenticate_request dependency,
    so the real auth middleware runs.  We only patch the outbound HTTP
    call to the OpenHands API (the external dependency).
    """

    async def test_valid_key_through_api(self, async_engine, async_session_factory):
        """Valid API key flows through real auth middleware to protected endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": 1,
            "name": "Test Key",
            "user_id": str(TEST_USER_ID),
            "org_id": str(TEST_ORG_ID),
            "auth_type": "bearer",
        }

        async def override_get_session():
            async with async_session_factory() as session:
                yield session

        # Only override the DB session; auth stays real
        app.dependency_overrides[get_session] = override_get_session
        app.state.engine = async_engine
        app.state.session_factory = async_session_factory

        # Create a mock http_client in app.state for the DI pattern
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False
        app.state.http_client = mock_client

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/automations",
                    headers={"Authorization": "Bearer real-key-123"},
                )

            assert response.status_code == 200
            data = response.json()
            assert "automations" in data
        finally:
            app.dependency_overrides.clear()

    async def test_missing_auth_header_through_api(
        self, async_engine, async_session_factory
    ):
        """Request without Authorization header is rejected by real auth middleware."""

        async def override_get_session():
            async with async_session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        app.state.engine = async_engine
        app.state.session_factory = async_session_factory

        # Create a mock http_client in app.state for the DI pattern
        mock_client = AsyncMock()
        mock_client.is_closed = False
        app.state.http_client = mock_client

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/automations")

            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    async def test_invalid_key_through_api(self, async_engine, async_session_factory):
        """Invalid API key is rejected after real auth middleware calls OpenHands."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        async def override_get_session():
            async with async_session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        app.state.engine = async_engine
        app.state.session_factory = async_session_factory

        # Create a mock http_client in app.state for the DI pattern
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False
        app.state.http_client = mock_client

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/automations",
                    headers={"Authorization": "Bearer bad-key"},
                )

            assert response.status_code == 401
            assert "Invalid or expired API key" in response.json()["detail"]
        finally:
            app.dependency_overrides.clear()


class TestRetryMechanism:
    """Tests for the retry mechanism on 429 rate limit responses using tenacity."""

    async def test_retry_on_429_then_success(self, mock_http_client):
        """Retries on 429 and succeeds when subsequent request returns 200."""
        mock_429_response = MagicMock()
        mock_429_response.status_code = 429

        mock_200_response = MagicMock()
        mock_200_response.status_code = 200

        mock_http_client.get = AsyncMock(
            side_effect=[mock_429_response, mock_200_response]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _make_auth_request_with_retry(
                mock_http_client,
                "http://test/api/keys/current",
                headers={"Authorization": "Bearer test"},
            )

        assert result.status_code == 200
        assert mock_http_client.get.call_count == 2

    async def test_retry_exhausted_returns_429(self, mock_http_client):
        """When all retries exhausted, returns the 429 response."""
        mock_429_response = MagicMock()
        mock_429_response.status_code = 429

        mock_http_client.get = AsyncMock(return_value=mock_429_response)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _make_auth_request_with_retry(
                mock_http_client,
                "http://test/api/keys/current",
                headers={"Authorization": "Bearer test"},
            )

        assert result.status_code == 429
        # Initial attempt + MAX_RETRIES (3) retries = 4 total calls
        assert mock_http_client.get.call_count == 4

    async def test_no_retry_on_non_429(self, mock_http_client):
        """Does not retry on non-429 status codes."""
        mock_401_response = MagicMock()
        mock_401_response.status_code = 401

        mock_http_client.get = AsyncMock(return_value=mock_401_response)

        result = await _make_auth_request_with_retry(
            mock_http_client,
            "http://test/api/keys/current",
            headers={"Authorization": "Bearer test"},
        )

        assert result.status_code == 401
        assert mock_http_client.get.call_count == 1

    async def test_authenticate_returns_429_after_retries(
        self, mock_request, mock_http_client
    ):
        """authenticate_request returns 429 when rate limited after retries."""
        mock_request.headers.get.return_value = "Bearer valid-key"

        mock_429_response = MagicMock()
        mock_429_response.status_code = 429

        mock_http_client.get = AsyncMock(return_value=mock_429_response)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 429
        assert "Rate limited" in exc_info.value.detail

    async def test_exponential_backoff(self, mock_http_client):
        """Verifies tenacity retries multiple times on 429."""
        mock_429_response = MagicMock()
        mock_429_response.status_code = 429

        mock_200_response = MagicMock()
        mock_200_response.status_code = 200

        mock_http_client.get = AsyncMock(
            side_effect=[
                mock_429_response,
                mock_429_response,
                mock_429_response,
                mock_200_response,
            ]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await _make_auth_request_with_retry(
                mock_http_client,
                "http://test/api/keys/current",
                headers={"Authorization": "Bearer test"},
            )

        assert result.status_code == 200
        # Tenacity uses exponential backoff, should have slept 3 times
        assert mock_sleep.call_count == 3
        # Verify backoff increases (tenacity uses 2^x * multiplier pattern)
        calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert calls[0] < calls[1] < calls[2]
