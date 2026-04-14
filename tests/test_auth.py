"""Tests for authentication module."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cachetools import TTLCache
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from automation.app import app
from automation.auth import (
    AUTH_CACHE_TTL_SECONDS,
    AuthenticatedUser,
    AuthMethod,
    _make_auth_request_with_retry,
    authenticate_request,
    clear_auth_cache,
    require_permission,
)
from automation.db import get_session


# Test UUIDs
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")

# Standard mock response matching GET /api/v1/users/me format
MOCK_USERS_ME_RESPONSE = {
    "id": str(TEST_USER_ID),
    "org_id": str(TEST_ORG_ID),
    "email": "test@example.com",
    "role": "owner",
    "permissions": ["view_org_settings", "manage_api_keys", "manage_automations"],
}


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear auth cache before and after each test."""
    clear_auth_cache()
    yield
    clear_auth_cache()


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request."""
    request = MagicMock()
    request.cookies = {}
    return request


@pytest.fixture
def mock_http_client():
    """Create a mock httpx client."""
    client = AsyncMock()
    client.is_closed = False
    return client


class TestAuthentication:
    """Tests for authenticate_request function with API key auth.

    These tests call authenticate_request directly with injected dependencies,
    bypassing FastAPI's DI system for unit testing.
    """

    async def test_authenticate_valid_api_key(self, mock_request, mock_http_client):
        """Valid API key returns AuthenticatedUser with correct fields."""
        mock_request.headers.get.return_value = "Bearer valid-api-key"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
        mock_http_client.get = AsyncMock(return_value=mock_response)

        result = await authenticate_request(mock_request, client=mock_http_client)

        assert isinstance(result, AuthenticatedUser)
        assert result.user_id == TEST_USER_ID
        assert result.org_id == TEST_ORG_ID
        assert result.email == "test@example.com"
        assert result.role == "owner"
        assert result.permissions == [
            "view_org_settings",
            "manage_api_keys",
            "manage_automations",
        ]
        assert result.auth_method == AuthMethod.API_KEY
        assert result.api_key == "valid-api-key"

    async def test_authenticate_missing_header(self, mock_request, mock_http_client):
        """Missing Authorization header and no cookie raises 401."""
        mock_request.headers.get.return_value = ""

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    async def test_authenticate_invalid_bearer_format(
        self, mock_request, mock_http_client
    ):
        """Invalid Bearer format with no cookie raises 401."""
        mock_request.headers.get.return_value = "InvalidFormat token"

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401

    async def test_authenticate_empty_bearer_token(
        self, mock_request, mock_http_client
    ):
        """Bearer prefix with empty token raises 401."""
        mock_request.headers.get.return_value = "Bearer "

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401
        assert "Empty API key" in exc_info.value.detail

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
        assert "Failed to reach OpenHands API" in exc_info.value.detail

    async def test_authenticate_unexpected_status(self, mock_request, mock_http_client):
        """Unexpected status code from OpenHands API raises 502."""
        mock_request.headers.get.return_value = "Bearer valid-key"

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_http_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 502


class TestCookieAuthentication:
    """Tests for authenticate_request function with cookie auth."""

    async def test_authenticate_valid_cookie(self, mock_request, mock_http_client):
        """Valid keycloak_auth cookie returns AuthenticatedUser."""
        mock_request.headers.get.return_value = ""
        mock_request.cookies = {"keycloak_auth": "valid-cookie-value"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
        mock_http_client.get = AsyncMock(return_value=mock_response)

        result = await authenticate_request(mock_request, client=mock_http_client)

        assert isinstance(result, AuthenticatedUser)
        assert result.user_id == TEST_USER_ID
        assert result.org_id == TEST_ORG_ID
        assert result.email == "test@example.com"
        assert result.auth_method == AuthMethod.COOKIE
        assert result.api_key is None

        # Verify outbound request used Cookie header
        call_args = mock_http_client.get.call_args
        headers = call_args[1]["headers"]
        assert "Cookie" in headers
        assert headers["Cookie"] == "keycloak_auth=valid-cookie-value"
        assert "Authorization" not in headers

    async def test_cookie_invalid_raises_401(self, mock_request, mock_http_client):
        """Invalid cookie (401 from OpenHands) raises 401."""
        mock_request.headers.get.return_value = ""
        mock_request.cookies = {"keycloak_auth": "bad-cookie"}

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_http_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401
        assert "Invalid or expired session cookie" in exc_info.value.detail

    async def test_api_key_takes_priority_over_cookie(
        self, mock_request, mock_http_client
    ):
        """When both Bearer token and cookie are present, API key wins."""
        mock_request.headers.get.return_value = "Bearer api-key-value"
        mock_request.cookies = {"keycloak_auth": "cookie-value"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
        mock_http_client.get = AsyncMock(return_value=mock_response)

        result = await authenticate_request(mock_request, client=mock_http_client)

        assert result.auth_method == AuthMethod.API_KEY
        assert result.api_key == "api-key-value"

        # Verify outbound request used Authorization header
        call_args = mock_http_client.get.call_args
        headers = call_args[1]["headers"]
        assert "Authorization" in headers
        assert "Cookie" not in headers

    async def test_no_auth_raises_401(self, mock_request, mock_http_client):
        """No Bearer token AND no cookie raises 401."""
        mock_request.headers.get.return_value = ""
        mock_request.cookies = {}

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_request(mock_request, client=mock_http_client)

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    async def test_cookie_openhands_unavailable(self, mock_request, mock_http_client):
        """Connection error to OpenHands API with cookie auth raises 502."""
        mock_request.headers.get.return_value = ""
        mock_request.cookies = {"keycloak_auth": "valid-cookie"}
        mock_http_client.get = AsyncMock(
            side_effect=httpx.RequestError("Connection failed")
        )

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
        """Valid API key flows through auth middleware to endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE

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
                    "/api/automation/v1",
                    headers={"Authorization": "Bearer real-key-123"},
                )

            assert response.status_code == 200
            data = response.json()
            assert "automations" in data
        finally:
            app.dependency_overrides.clear()

    async def test_valid_cookie_through_api(self, async_engine, async_session_factory):
        """Valid keycloak_auth cookie flows through auth middleware to endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE

        async def override_get_session():
            async with async_session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        app.state.engine = async_engine
        app.state.session_factory = async_session_factory

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False
        app.state.http_client = mock_client

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/automation/v1",
                    cookies={"keycloak_auth": "valid-cookie-123"},
                )

            assert response.status_code == 200
            data = response.json()
            assert "automations" in data
        finally:
            app.dependency_overrides.clear()

    async def test_missing_auth_header_through_api(
        self, async_engine, async_session_factory
    ):
        """Request without Authorization header or cookie is rejected."""

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
                response = await client.get("/api/automation/v1")

            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    async def test_invalid_key_through_api(self, async_engine, async_session_factory):
        """Invalid API key is rejected by auth middleware."""
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
                    "/api/automation/v1",
                    headers={"Authorization": "Bearer bad-key"},
                )

            assert response.status_code == 401
            assert "Invalid or expired API key" in response.json()["detail"]
        finally:
            app.dependency_overrides.clear()


class TestAuthCache:
    """Tests for authentication caching functionality."""

    async def test_cache_hit_skips_api_call(self, mock_request, mock_http_client):
        """Second call with same API key uses cache and skips API call."""
        mock_request.headers.get.return_value = "Bearer cached-key"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
        mock_http_client.get = AsyncMock(return_value=mock_response)

        # First call - should hit API
        result1 = await authenticate_request(mock_request, client=mock_http_client)
        assert mock_http_client.get.call_count == 1

        # Second call - should use cache
        result2 = await authenticate_request(mock_request, client=mock_http_client)
        assert mock_http_client.get.call_count == 1  # No additional API call

        assert result1.user_id == result2.user_id
        assert result1.org_id == result2.org_id

    async def test_cache_expires_after_ttl(self, mock_request, mock_http_client):
        """Cache entry expires after TTL and API is called again."""
        import asyncio

        import automation.auth as auth_module

        # Use a short TTL for testing (0.5 seconds)
        test_ttl = 0.5
        original_cache = auth_module._auth_cache
        auth_module._auth_cache = TTLCache(maxsize=1024, ttl=test_ttl)

        try:
            mock_request.headers.get.return_value = "Bearer expiring-key"

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
            mock_http_client.get = AsyncMock(return_value=mock_response)

            # First call
            await authenticate_request(mock_request, client=mock_http_client)
            assert mock_http_client.get.call_count == 1

            # Wait for TTL to expire (add buffer for timing)
            await asyncio.sleep(test_ttl + 0.1)

            # Second call after expiry - should hit API again
            await authenticate_request(mock_request, client=mock_http_client)
            assert mock_http_client.get.call_count == 2
        finally:
            auth_module._auth_cache = original_cache

    async def test_different_keys_cached_separately(
        self, mock_request, mock_http_client
    ):
        """Different API keys are cached independently."""
        user2_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        org2_id = uuid.UUID("33333333-3333-3333-3333-333333333333")

        mock_response1 = MagicMock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = MOCK_USERS_ME_RESPONSE

        mock_response2 = MagicMock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = {
            "id": str(user2_id),
            "org_id": str(org2_id),
            "email": "user2@example.com",
            "role": "member",
            "permissions": [],
        }

        mock_http_client.get = AsyncMock(side_effect=[mock_response1, mock_response2])

        # First key
        mock_request.headers.get.return_value = "Bearer key-1"
        result1 = await authenticate_request(mock_request, client=mock_http_client)

        # Second key
        mock_request.headers.get.return_value = "Bearer key-2"
        result2 = await authenticate_request(mock_request, client=mock_http_client)

        assert mock_http_client.get.call_count == 2
        assert result1.user_id == TEST_USER_ID
        assert result2.user_id == user2_id

    async def test_cookie_and_api_key_cached_separately(
        self, mock_request, mock_http_client
    ):
        """Cookie auth and API key auth are cached with different keys."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
        mock_http_client.get = AsyncMock(return_value=mock_response)

        # Authenticate with API key
        mock_request.headers.get.return_value = "Bearer some-credential"
        mock_request.cookies = {}
        result1 = await authenticate_request(mock_request, client=mock_http_client)
        assert mock_http_client.get.call_count == 1

        # Authenticate with cookie using same credential string
        # (different hash because credential value differs in practice,
        # but even same string would be cached separately due to different hash input)
        mock_request.headers.get.return_value = ""
        mock_request.cookies = {"keycloak_auth": "some-cookie-value"}
        result2 = await authenticate_request(mock_request, client=mock_http_client)
        assert mock_http_client.get.call_count == 2  # Cache miss, different credential

        assert result1.auth_method == AuthMethod.API_KEY
        assert result2.auth_method == AuthMethod.COOKIE

    async def test_cookie_cache_hit(self, mock_request, mock_http_client):
        """Second call with same cookie uses cache and skips API call."""
        mock_request.headers.get.return_value = ""
        mock_request.cookies = {"keycloak_auth": "cached-cookie"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_USERS_ME_RESPONSE
        mock_http_client.get = AsyncMock(return_value=mock_response)

        # First call
        result1 = await authenticate_request(mock_request, client=mock_http_client)
        assert mock_http_client.get.call_count == 1

        # Second call - should use cache
        result2 = await authenticate_request(mock_request, client=mock_http_client)
        assert mock_http_client.get.call_count == 1

        assert result1.user_id == result2.user_id

    async def test_failed_auth_not_cached(self, mock_request, mock_http_client):
        """Failed authentication attempts are not cached."""
        mock_request.headers.get.return_value = "Bearer bad-key"

        mock_401_response = MagicMock()
        mock_401_response.status_code = 401
        mock_http_client.get = AsyncMock(return_value=mock_401_response)

        # First attempt - should fail
        with pytest.raises(HTTPException):
            await authenticate_request(mock_request, client=mock_http_client)

        # Second attempt - should still call API (not cached)
        with pytest.raises(HTTPException):
            await authenticate_request(mock_request, client=mock_http_client)

        assert mock_http_client.get.call_count == 2

    def test_cache_ttl_is_20_seconds(self):
        """Verify the cache TTL is set to 20 seconds."""
        assert AUTH_CACHE_TTL_SECONDS == 20.0


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
                "http://test/api/v1/users/me",
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
                "http://test/api/v1/users/me",
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
            "http://test/api/v1/users/me",
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
                "http://test/api/v1/users/me",
                headers={"Authorization": "Bearer test"},
            )

        assert result.status_code == 200
        # Tenacity uses exponential backoff, should have slept 3 times
        assert mock_sleep.call_count == 3
        # Verify backoff increases (tenacity uses 2^x * multiplier pattern)
        calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert calls[0] < calls[1] < calls[2]


class TestRequirePermission:
    """Tests for require_permission factory function."""

    async def test_permission_present_returns_user(self):
        """User with the required permission is returned successfully."""
        user = AuthenticatedUser(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            email="test@example.com",
            role="owner",
            permissions=["manage_automations", "view_org_settings"],
            auth_method=AuthMethod.API_KEY,
            api_key="key",
        )

        checker = require_permission("manage_automations")
        result = await checker(user=user)

        assert result is user

    async def test_permission_missing_raises_403(self):
        """User without the required permission gets HTTP 403."""
        user = AuthenticatedUser(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            email="test@example.com",
            role="member",
            permissions=["view_org_settings"],
            auth_method=AuthMethod.API_KEY,
            api_key="key",
        )

        checker = require_permission("manage_automations")

        with pytest.raises(HTTPException) as exc_info:
            await checker(user=user)

        assert exc_info.value.status_code == 403
        assert "manage_automations" in exc_info.value.detail

    async def test_different_permission_raises_403(self):
        """Having a different permission does not satisfy the requirement."""
        user = AuthenticatedUser(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            email="test@example.com",
            role="member",
            permissions=["some_other_permission"],
            auth_method=AuthMethod.API_KEY,
            api_key="key",
        )

        checker = require_permission("manage_automations")

        with pytest.raises(HTTPException) as exc_info:
            await checker(user=user)

        assert exc_info.value.status_code == 403
