"""Authentication for the automations service API.

Supports three authentication methods (checked in order):
1. API key via Authorization: Bearer header
2. API key via X-Session-API-Key header (matches agent-server convention,
   useful behind reverse proxies that overwrite the Authorization header)
3. Cookie: keycloak_auth cookie from the OpenHands web UI

All methods validate against the OpenHands API GET /api/v1/users/me endpoint
to get the user and organization identity.
"""

import hashlib
import logging
import secrets
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

from openhands.automation.config import get_config


logger = logging.getLogger("automation.auth")

# Auth cache - initialized lazily to use config values
_auth_cache: TTLCache[str, "AuthenticatedUser"] | None = None
SESSION_COOKIE_NAME = "keycloak_auth"
# Keep parity with OpenHands' cookie chunking helper: 8 * 3000 bytes is
# comfortably above expected session token sizes while staying bounded.
MAX_SESSION_COOKIE_CHUNKS = 8


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


def _reset_auth_cache() -> None:
    """Reset the auth cache so it will be recreated with new config values.

    Called by clear_config_cache() to ensure tests that change config see
    the new cache settings take effect.
    """
    global _auth_cache
    _auth_cache = None


class AuthMethod(StrEnum):
    """Authentication method used for the request."""

    API_KEY = "api_key"
    COOKIE = "cookie"
    LOCAL_API_KEY = "local_api_key"


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
    model_profile_names: frozenset[str] | None = None
    active_model_profile_name: str | None = None


def _extract_model_profile_names(data: dict) -> frozenset[str] | None:  # type: ignore[type-arg]
    """Extract model profile names from a users/me response when present."""
    profiles_payload = data.get("llm_profiles")
    if not isinstance(profiles_payload, dict):
        return None

    profiles = profiles_payload.get("profiles")
    if isinstance(profiles, dict):
        return frozenset(str(name) for name in profiles)
    if isinstance(profiles, list):
        names = {
            str(profile["name"])
            for profile in profiles
            if isinstance(profile, dict) and isinstance(profile.get("name"), str)
        }
        return frozenset(names) if names else None

    return None


def _extract_active_model_profile_name(data: dict) -> str | None:  # type: ignore[type-arg]
    """Extract the active model profile name from a users/me response when present."""
    profiles_payload = data.get("llm_profiles")
    if not isinstance(profiles_payload, dict):
        return None

    active_profile = profiles_payload.get("active_profile")
    return active_profile if isinstance(active_profile, str) else None


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


# Module-level retry decorator for auth requests.
# Config is read at import time and frozen for the process lifetime.
_http_config = get_config().http
_auth_retry = retry(
    retry=retry_if_result(_is_rate_limited),
    stop=stop_after_attempt(_http_config.auth_max_retries + 1),
    wait=wait_exponential(
        multiplier=_http_config.auth_initial_backoff,
        max=_http_config.auth_max_backoff,
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    retry_error_callback=_return_last_response,
)


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


def _get_local_user() -> AuthenticatedUser:
    """Return a default user for local mode authentication.

    Used when authenticating with local_api_key in self-hosted deployments.
    Provides deterministic user/org IDs for consistent data ownership.

    Security notes:
    - Uses deterministic UUID5 based on DNS namespace, meaning every self-hosted
      installation gets identical user/org IDs. This ensures consistent data
      ownership tracking across service restarts.
    - If logs or database exports containing these IDs are shared between
      separate installations, data attribution could be ambiguous. For isolated
      deployments (the typical self-hosted case), this is acceptable.

    Access model:
    - Grants admin role with manage_automations permission, giving full access.
    - Self-hosted deployments typically have full trust in their environment,
      so permissive defaults are appropriate. Read-only or restricted access
      modes could be added later if needed via additional config options.
    """
    # Use deterministic UUIDs based on namespace (consistent across restarts)
    local_user_id = uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-user")
    local_org_id = uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-org")

    return AuthenticatedUser(
        user_id=local_user_id,
        org_id=local_org_id,
        email="local@localhost",
        role="admin",
        permissions=["manage_automations"],
        auth_method=AuthMethod.LOCAL_API_KEY,
        api_key=None,
    )


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


def _extract_api_key(request: Request) -> str | None:
    """Extract an API key from the request headers.

    Checks Authorization: Bearer first, then X-Session-API-Key as a fallback
    (useful behind reverse proxies that overwrite the Authorization header).
    Returns None when neither header carries a key.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header.removeprefix("Bearer ").strip()
        if not key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Empty API key",
            )
        return key

    session_key = request.headers.get("X-Session-API-Key", "").strip()
    return session_key or None


def _extract_session_cookie(request: Request) -> str | None:
    """Read OpenHands' possibly chunked session cookie.

    OpenHands splits large keycloak_auth values across keycloak_auth,
    keycloak_auth_1, keycloak_auth_2, etc. Automation validates by forwarding
    the reassembled token back to OpenHands /api/v1/users/me.
    """
    first = request.cookies.get(SESSION_COOKIE_NAME)
    if not first:
        return None

    parts = [first]
    for index in range(1, MAX_SESSION_COOKIE_CHUNKS):
        part = request.cookies.get(f"{SESSION_COOKIE_NAME}_{index}")
        if part is None:
            break
        parts.append(part)
    return "".join(parts)


def _extract_credential(request: Request) -> tuple[str, AuthMethod]:
    """Extract a credential and its auth method from the request.

    Priority: Authorization: Bearer → X-Session-API-Key → keycloak_auth cookie.
    Raises 401 if nothing usable is found.
    """
    api_key = _extract_api_key(request)
    if api_key:
        return api_key, AuthMethod.API_KEY

    cookie = _extract_session_cookie(request)
    if cookie:
        return cookie, AuthMethod.COOKIE

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide Bearer token, "
        "X-Session-API-Key header, or keycloak_auth cookie",
    )


def _parse_users_me(
    data: dict, auth_method: AuthMethod, credential: str
) -> AuthenticatedUser:  # type: ignore[type-arg]
    """Build an AuthenticatedUser from the OpenHands /api/v1/users/me response."""
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

    return AuthenticatedUser(
        user_id=user_uuid,
        org_id=org_uuid,
        email=data.get("email", ""),
        role=data.get("role", ""),
        permissions=data.get("permissions", []),
        auth_method=auth_method,
        api_key=credential if auth_method == AuthMethod.API_KEY else None,
        model_profile_names=_extract_model_profile_names(data),
        active_model_profile_name=_extract_active_model_profile_name(data),
    )


async def authenticate_request(
    request: Request,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> AuthenticatedUser:
    """Authenticate via API key (Bearer / X-Session-API-Key) or cookie.

    Local mode: only the configured ``local_api_key`` is accepted;
    SaaS validation is skipped entirely.

    SaaS mode: credentials are verified against ``GET /api/v1/users/me``
    with retry + in-memory caching.
    """
    settings = get_config().service

    # --- Local-mode fast path (no network call) ---
    api_key = _extract_api_key(request)
    if api_key and settings.is_local_mode and settings.local_api_key:
        if secrets.compare_digest(api_key, settings.local_api_key):
            logger.debug("Authenticated via local API key")
            return _get_local_user()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # --- Resolve credential (API key or cookie) ---
    credential, auth_method = _extract_credential(request)

    # --- Cache lookup ---
    cache_key = _credential_cache_key(credential)
    auth_cache = _get_auth_cache()
    cached_user = auth_cache.get(cache_key)
    if cached_user is not None:
        logger.debug("Auth cache hit for user %s", cached_user.user_id)
        return cached_user

    # --- Validate against OpenHands API ---
    logger.debug("Auth cache miss, validating with OpenHands API")
    outbound_headers = (
        {"Authorization": f"Bearer {credential}"}
        if auth_method == AuthMethod.API_KEY
        else {"Cookie": f"{SESSION_COOKIE_NAME}={credential}"}
    )

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
        detail = (
            "Invalid or expired API key"
            if auth_method == AuthMethod.API_KEY
            else "Invalid or expired session cookie"
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
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

    # --- Build user and cache ---
    user = _parse_users_me(resp.json(), auth_method, credential)
    auth_cache[cache_key] = user
    return user
