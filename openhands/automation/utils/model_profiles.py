"""Helpers for resolving and validating model profile selections."""

import logging
import uuid
from urllib.parse import quote

import httpx
from cachetools import TTLCache
from fastapi import HTTPException, Request, status

from openhands.automation.auth import (
    X_ORG_ID_HEADER,
    AuthenticatedUser,
    get_http_client,
    upstream_auth_headers,
)


logger = logging.getLogger(__name__)

# The profile ids an organization was last seen to have. A setup form validates
# the same selection over and over, and only a hit is trusted: an id that is
# not here is always looked up, so a profile created a moment ago is found.
_known_profiles: TTLCache[uuid.UUID, frozenset[str]] = TTLCache(maxsize=1024, ttl=60)


def validate_model_profile_for_user(
    model_profile: str | None, user: AuthenticatedUser
) -> None:
    """Validate a selected model profile against authenticated user metadata.

    Profile metadata is available only when the upstream auth response includes
    `llm_profiles`. If it is absent (for example, local mode or older upstream
    responses), runtime profile lookup remains the source of truth.
    """
    if not model_profile or user.model_profile_names is None:
        return

    if model_profile not in user.model_profile_names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Model profile `{model_profile}` not found",
        )


def resolve_model_profile_for_user(
    requested_profile: str | None, user: AuthenticatedUser
) -> str | None:
    """Resolve the profile name an automation should persist.

    Automations store model profile names, not raw LLM settings. If the request does
    not specify a profile, use the user's active profile at creation/update time.
    Older/local auth responses may not include profile metadata; in that case we
    leave the value unset so existing fallback behavior can preserve compatibility.
    """
    model_profile = requested_profile or user.active_model_profile_name
    validate_model_profile_for_user(model_profile, user)
    return model_profile


def validate_agent_profile_combination(
    agent_profile_id: uuid.UUID | None, model: str | None
) -> None:
    """An agent profile owns its model, so the two cannot be selected together."""
    if agent_profile_id is not None and model:
        raise HTTPException(422, "An agent profile already specifies the model")


def validate_agent_profile_selection(
    agent_profile_id: uuid.UUID | None, model: str | None
) -> None:
    """Validate a profile selected where no caller is present, as in git sync.

    Without a caller there is no credential to check the profile against the
    OpenHands app server with, so in cloud mode the selection is refused. A
    caller's own selection goes through `ensure_agent_profile_exists` instead.
    """
    if agent_profile_id is None:
        return
    from openhands.automation.config import get_config

    if not get_config().service.is_local_mode:
        raise HTTPException(
            422,
            "Agent profiles can only be selected through the API on this deployment",
        )
    validate_agent_profile_combination(agent_profile_id, model)



def _agent_profiles_base_url() -> str:
    from openhands.automation.config import get_config

    settings = get_config().service
    if settings.is_local_mode:
        return settings.agent_server_url.rstrip("/")
    return settings.openhands_api_base_url.rstrip("/")


def _agent_profiles_headers(
    request: Request, user: AuthenticatedUser
) -> dict[str, str]:
    from openhands.automation.config import get_config

    settings = get_config().service
    if settings.is_local_mode:
        session_key = request.headers.get("X-Session-API-Key", "").strip()
        if session_key:
            return {"X-Session-API-Key": session_key}
    return {**upstream_auth_headers(request), X_ORG_ID_HEADER: str(user.org_id)}


async def _list_agent_profiles(
    request: Request,
    user: AuthenticatedUser,
) -> list[dict]:
    """Fetch agent profile summaries for the caller's organization."""
    headers = _agent_profiles_headers(request, user)
    try:
        resp = await get_http_client(request).get(
            f"{_agent_profiles_base_url()}/api/agent-profiles", headers=headers
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Failed to reach OpenHands API for agent profiles",
        ) from exc
    if resp.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise HTTPException(
            resp.status_code, "OpenHands API refused the request for agent profiles"
        )
    try:
        if resp.status_code != status.HTTP_200_OK:
            raise ValueError(f"status {resp.status_code}")
        profiles = resp.json()["profiles"]
        if not isinstance(profiles, list):
            raise TypeError("profiles is not a list")
        return [profile for profile in profiles if isinstance(profile, dict)]
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Agent profile lookup failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected response from OpenHands API for agent profiles",
        ) from exc


def _validate_unique_agent_profile_names(profiles: list[dict]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for profile in profiles:
        name = profile.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Duplicate agent profile names found: " + ", ".join(sorted(duplicates)),
        )


def _parse_agent_profile_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected agent profile id in OpenHands API response",
        ) from exc


async def validate_agent_profile_allowed_tools(
    agent_profile_id: uuid.UUID | None,
    agent_profile_name: str | None,
    allowed_tools: list[str] | None,
    request: Request,
    user: AuthenticatedUser,
) -> uuid.UUID | None:
    """Reject missing/ambiguous profile names and disallowed explicit tools."""
    if agent_profile_id is None and not agent_profile_name:
        return None

    profiles = await _list_agent_profiles(request, user)
    _validate_unique_agent_profile_names(profiles)
    resolved_id = agent_profile_id
    resolved_name = agent_profile_name
    if agent_profile_id is not None:
        agent_profile_id_str = str(agent_profile_id)
        resolved_name = next(
            (
                profile.get("name")
                for profile in profiles
                if str(profile.get("id")) == agent_profile_id_str
            ),
            None,
        )
        if resolved_name is None:
            raise HTTPException(422, f"Agent profile `{agent_profile_id}` not found")
    elif agent_profile_name:
        selected = next(
            (
                profile
                for profile in profiles
                if profile.get("name") == agent_profile_name
            ),
            None,
        )
        if selected is None:
            raise HTTPException(422, f"Agent profile `{agent_profile_name}` not found")
        resolved_id = _parse_agent_profile_id(selected.get("id"))
    if not resolved_name or resolved_id is None:
        raise HTTPException(422, "Plugin automation requires an agent profile")

    if allowed_tools is None:
        return resolved_id

    headers = _agent_profiles_headers(request, user)
    try:
        resp = await get_http_client(request).get(
            f"{_agent_profiles_base_url()}/api/agent-profiles/"
            f"{quote(resolved_name, safe='')}",
            headers=headers,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Failed to reach OpenHands API for agent profile details",
        ) from exc
    if resp.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise HTTPException(
            resp.status_code, "OpenHands API refused the request for agent profiles"
        )
    if resp.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(422, f"Agent profile `{resolved_name}` not found")
    try:
        if resp.status_code != status.HTTP_200_OK:
            raise ValueError(f"status {resp.status_code}")
        body = resp.json()
        profile = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(profile, dict):
            raise TypeError("profile is not an object")
    except (ValueError, TypeError) as exc:
        logger.warning("Agent profile detail lookup failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected response from OpenHands API for agent profile details",
        ) from exc

    tools = profile.get("tools")
    if tools is None:
        return resolved_id
    if not isinstance(tools, list):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected tools field in OpenHands API agent profile response",
        )
    selected_tools = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict) and tool.get("name")
    }
    disallowed = sorted(selected_tools - set(allowed_tools))
    if disallowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            (
                f"Agent profile `{resolved_name}` enables tools not allowed by "
                f"this automation plugin: {', '.join(disallowed)}"
            ),
        )
    return resolved_id


async def ensure_agent_profile_exists(
    agent_profile_id: uuid.UUID | None, request: Request, user: AuthenticatedUser
) -> None:
    """Check a profile the caller selected against their organization.

    A local deployment hands the id to its Agent Server, which resolves it. In
    cloud mode the profiles belong to the caller's organization in the
    OpenHands app server, which starts a conversation with default settings
    rather than fail on an id it does not know, so an unknown id is refused
    here. A profile deleted after this check still falls back at run time.

    A miss asks the app server, so a caller that has a database transaction
    open holds its connection for that long.
    """
    if agent_profile_id is None:
        return
    from openhands.automation.config import get_config

    settings = get_config().service
    if settings.is_local_mode:
        return
    if str(agent_profile_id) in _known_profiles.get(user.org_id, ()):
        return

    # The organization the automation is stored under, not whichever one the
    # caller's session happens to resolve to by now.
    headers = {**upstream_auth_headers(request), X_ORG_ID_HEADER: str(user.org_id)}
    try:
        resp = await get_http_client(request).get(
            f"{settings.openhands_api_base_url}/api/agent-profiles", headers=headers
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Failed to reach OpenHands API for agent profiles",
        ) from exc
    if resp.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        # The credential was accepted from the auth cache but the app server no
        # longer honours it; say so rather than report a gateway fault.
        raise HTTPException(
            resp.status_code, "OpenHands API refused the request for agent profiles"
        )
    try:
        if resp.status_code != status.HTTP_200_OK:
            raise ValueError(f"status {resp.status_code}")
        profiles = resp.json()["profiles"]
        known = frozenset(
            str(profile["id"])
            for profile in profiles
            if isinstance(profile, dict) and profile.get("id")
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Agent profile lookup failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected response from OpenHands API for agent profiles",
        ) from exc
    _known_profiles[user.org_id] = known
    if str(agent_profile_id) not in known:
        raise HTTPException(422, f"Agent profile `{agent_profile_id}` not found")


def clear_agent_profile_cache() -> None:
    """Forget the profiles seen so far. Tests use it to start clean."""
    _known_profiles.clear()
