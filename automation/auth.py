"""Authentication for the automations service API.

MVP approach: The caller passes an OpenHands API key in the Authorization header.
We validate it against the OpenHands API /api/keys/current endpoint to get
the user and organization identity.
"""

import logging
import uuid
from dataclasses import dataclass

import httpx
from cachetools import TTLCache
from fastapi import Depends, HTTPException, Request, status
from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from automation.config import get_settings


logger = logging.getLogger("automation.auth")

# Cache TTL in seconds
AUTH_CACHE_TTL_SECONDS = 20

# In-memory cache for authenticated users with 20 second TTL
_auth_cache: TTLCache[str, "AuthenticatedUser"] = TTLCache(maxsize=1024, ttl=AUTH_CACHE_TTL_SECONDS)

# Default timeout for HTTP client
HTTP_CLIENT_TIMEOUT = 10.0

# Retry configuration for rate limiting
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0


def create_http_client() -> httpx.AsyncClient:
    """Create a new httpx client for auth requests."""
    return httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)


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
    api_key: str  # The raw API key (needed for downstream API calls)


def clear_auth_cache() -> None:
    """Clear all cached authentication data. Useful for testing."""
    _auth_cache.clear()


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


@retry(
    retry=retry_if_result(_is_rate_limited),
    stop=stop_after_attempt(MAX_RETRIES + 1),
    wait=wait_exponential(
        multiplier=INITIAL_BACKOFF_SECONDS,
        max=MAX_BACKOFF_SECONDS,
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    retry_error_callback=_return_last_response,
)
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


async def authenticate_request(
    request: Request,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> AuthenticatedUser:
    """Extract and validate the OpenHands API key from the Authorization header.

    Calls the OpenHands API /api/keys/current to verify the key and get
    user/org identity. Implements retry with exponential backoff for rate limiting.
    Results are cached in-memory for 20 seconds to reduce API calls.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. "
            "Expected: Bearer <api_key>",
        )

    api_key = auth_header.removeprefix("Bearer ").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty API key",
        )

    # Check cache first
    cached_user = _auth_cache.get(api_key)
    if cached_user is not None:
        logger.debug("Auth cache hit for user %s", cached_user.user_id)
        return cached_user

    settings = get_settings()
    try:
        resp = await _make_auth_request_with_retry(
            client,
            f"{settings.openhands_api_base_url}/api/keys/current",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except httpx.RequestError as e:
        logger.error("Failed to reach OpenHands API for auth: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to validate API key against OpenHands",
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
        )
    if resp.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limited by authentication service",
        )
    if resp.status_code != 200:
        logger.error(
            "Unexpected status from OpenHands /api/keys/current: %s",
            resp.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from OpenHands API",
        )

    data = resp.json()
    user_id = data.get("user_id")
    org_id = data.get("org_id")
    if not user_id or not org_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not determine user/org identity from OpenHands API",
        )

    try:
        user_uuid = uuid.UUID(str(user_id))
        org_uuid = uuid.UUID(str(org_id))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid user_id or org_id format from OpenHands API",
        )

    user = AuthenticatedUser(user_id=user_uuid, org_id=org_uuid, api_key=api_key)
    _auth_cache[api_key] = user
    return user
