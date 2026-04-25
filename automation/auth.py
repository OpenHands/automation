"""Authentication for the automations service API.

Supports two authentication methods:
1. API key: Bearer token in the Authorization header
2. Cookie: keycloak_auth cookie from the OpenHands web UI

Both methods validate against the OpenHands API GET /api/v1/users/me endpoint
to get the user and organization identity.
"""

import hashlib
import logging
import uuid
from enum import StrEnum

import httpx
from cachetools import TTLCache
from fastapi import Depends, HTTPException, Request, status
from pydantic.dataclasses import dataclass
from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from automation.config import get_config


logger = logging.getLogger("automation.auth")

# Retry configuration for rate limiting
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0

# Auth cache - initialized lazily to use config values
_auth_cache: TTLCache[str, "AuthenticatedUser"] | None = None


def _get_auth_cache() -> TTLCache[str, "AuthenticatedUser"]:
    """Get or create the auth cache with config-based settings."""
    global _auth_cache
    if _auth_cache is None:
        http_config = get_config().http
        _auth_cache = TTLCache(
            maxsize=http_config.auth_cache_size,
            ttl=http_config.auth_cache_ttl,
        )
    return _auth_cache


class AuthMethod(StrEnum):
    """Authentication method used for the request."""

    API_KEY = "api_key"
    COOKIE = "cookie"


def create_http_client() -> httpx.AsyncClient:
    """Create a new httpx client for auth requests."""
    return httpx.AsyncClient(timeout=get_config().http.http_timeout)


def get_http_client(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency to get the shared httpx client from app.state.

    The client is created during app startup and stored in app.state.http_client.
    This enables proper dependency injection and makes testing easier.
    """
    client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if client is None or client.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HTTP client not initialized",
        )
    return client


@dataclass
class AuthenticatedUser:
    user_id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    permissions: list[str]
    auth_method: AuthMethod
    api_key: str | None = None  # Set when auth_method == API_KEY


def clear_auth_cache() -> None:
    """Clear all cached authentication data. Useful for testing."""
    _get_auth_cache().clear()


def _credential_cache_key(credential: str) -> str:
    """Hash a credential for use as a cache key (never store raw credential)."""
    return hashlib.sha256(credential.encode()).hexdigest()


def _is_rate_limited(response: httpx.Response) -> bool:
    """Check if response is a 429 rate limit response."""
    return response.status_code == 429


def _return_last_response(retry_state: RetryCallState) -> httpx.Response:
    """Return the last response when retries are exhausted."""
    logger.warning(
        "Rate limit retries exhausted after %d attempts",
        retry_state.attempt_number,
    )
    # Defensive check: outcome should be set by tenacity, but guard against
    # potential library changes or edge cases for type safety
    if retry_state.outcome is None:
        raise RuntimeError("retry_error_callback invoked without outcome")
    return retry_state.outcome.result()


def _get_auth_retry_decorator():
    """Build retry decorator with current config values."""
    http_config = get_config().http
    return retry(
        retry=retry_if_result(_is_rate_limited),
        stop=stop_after_attempt(MAX_RETRIES + 1),
        wait=wait_exponential(
            multiplier=INITIAL_BACKOFF_SECONDS,
            max=http_config.max_backoff,
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        retry_error_callback=_return_last_response,
    )


# Module-level decorator (uses config at import time)
_auth_retry = _get_auth_retry_decorator()


@_auth_retry
async def _make_auth_request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    """Make an auth request with exponential backoff retry on 429 responses.

    Uses tenacity for retry logic with exponential backoff.

    Args:
        client: The httpx client to use for requests
        url: The URL to request
        headers: Request headers

    Returns:
        The HTTP response (may still be a 429 if all retries exhausted)

    Raises:
        httpx.RequestError: If there's a network/connection error
    """
    return await client.get(url, headers=headers)


def require_permission(permission: str):
    """Factory that returns a FastAPI dependency enforcing a permission.

    Checks whether the authenticated user has the given permission string
    in their permissions list.  Raises HTTP 403 if missing, otherwise
    returns the ``AuthenticatedUser``.
    """

    async def _check(
        user: "AuthenticatedUser" = Depends(authenticate_request),
    ) -> "AuthenticatedUser":
        if permission not in user.permissions:
            logger.warning(
                "Permission denied: user %s missing permission %s",
                user.user_id,
                permission,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {permission} permission",
            )
        return user

    return _check


async def authenticate_request(
    request: Request,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> AuthenticatedUser:
    """Authenticate the request using API key or keycloak_auth cookie.

    Supports two authentication methods (checked in priority order):
    1. API key via Authorization: Bearer <api_key> header
    2. Cookie via keycloak_auth cookie

    Calls the OpenHands API GET /api/v1/users/me to verify credentials and get
    user/org identity. Implements retry with exponential backoff for rate limiting.
    Results are cached in-memory for 20 seconds to reduce API calls.
    """
    # Determine authentication method (API key takes priority)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header.removeprefix("Bearer ").strip()
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Empty API key",
            )
        auth_method = AuthMethod.API_KEY
        credential = api_key
    else:
        cookie_value = request.cookies.get("keycloak_auth")
        if cookie_value:
            auth_method = AuthMethod.COOKIE
            credential = cookie_value
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required: provide Bearer token "
                "or keycloak_auth cookie",
            )

    # Check cache first
    cache_key = _credential_cache_key(credential)
    auth_cache = _get_auth_cache()
    cached_user = auth_cache.get(cache_key)
    if cached_user is not None:
        logger.debug("Auth cache hit for user %s", cached_user.user_id)
        return cached_user

    logger.debug("Auth cache miss, validating with OpenHands API")

    # Build outbound headers based on auth method
    settings = get_config().service
    if auth_method == AuthMethod.API_KEY:
        outbound_headers = {"Authorization": f"Bearer {credential}"}
    else:
        outbound_headers = {"Cookie": f"keycloak_auth={credential}"}

    try:
        resp = await _make_auth_request_with_retry(
            client,
            f"{settings.openhands_api_base_url}/api/v1/users/me",
            headers=outbound_headers,
        )
    except httpx.RequestError as e:
        logger.error("Failed to reach OpenHands API for auth: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach OpenHands API for authentication",
        )

    if resp.status_code == 401:
        if auth_method == AuthMethod.API_KEY:
            detail = "Invalid or expired API key"
        else:
            detail = "Invalid or expired session cookie"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
        )
    if resp.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limited by authentication service",
        )
    if resp.status_code != 200:
        logger.error(
            "Unexpected status from OpenHands /api/v1/users/me: %s",
            resp.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from OpenHands API",
        )

    data = resp.json()
    user_id_raw = data.get("id")
    org_id_raw = data.get("org_id")
    if not user_id_raw or not org_id_raw:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not determine user/org identity from OpenHands API",
        )

    try:
        user_uuid = uuid.UUID(str(user_id_raw))
        org_uuid = uuid.UUID(str(org_id_raw))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid user_id or org_id format from OpenHands API",
        )

    email = data.get("email", "")
    role = data.get("role", "")
    permissions = data.get("permissions", [])

    user = AuthenticatedUser(
        user_id=user_uuid,
        org_id=org_uuid,
        email=email,
        role=role,
        permissions=permissions,
        auth_method=auth_method,
        api_key=credential if auth_method == AuthMethod.API_KEY else None,
    )
    auth_cache[cache_key] = user
    return user
