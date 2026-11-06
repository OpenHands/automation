"""Helpers for resolving and validating model profile selections."""

import logging
import uuid
from collections import Counter
from urllib.parse import quote

import httpx
from cachetools import TTLCache
from fastapi import HTTPException, Request, status
from pydantic import ValidationError

from openhands.automation.auth import (
    X_ORG_ID_HEADER,
    AuthenticatedUser,
    get_http_client,
    upstream_auth_headers,
)
from openhands.sdk.profiles import (
    AgentProfileBase,
    AgentProfileInfo,
    AgentProfileListResponse,
    OpenHandsAgentProfile,
    validate_agent_profile,
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


def _agent_profiles_from_response(resp: httpx.Response) -> list[AgentProfileInfo]:
    return AgentProfileListResponse.model_validate(resp.json()).profiles


def _agent_profile_detail_from_response(resp: httpx.Response) -> AgentProfileBase:
    body = resp.json()
    profile = body.get("profile") if isinstance(body, dict) else None
    if not isinstance(profile, dict):
        raise TypeError("profile is not an object")
    return validate_agent_profile(profile)


async def _list_agent_profiles_from_api(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
) -> list[AgentProfileInfo]:
    try:
        resp = await client.get(f"{base_url}/api/agent-profiles", headers=headers)
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
        return _agent_profiles_from_response(resp)
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        logger.warning("Agent profile lookup failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected response from OpenHands API for agent profiles",
        ) from exc


async def _list_agent_profiles(
    request: Request,
    user: AuthenticatedUser,
) -> list[AgentProfileInfo]:
    """Fetch agent profile summaries for the caller's organization."""
    return await _list_agent_profiles_from_api(
        get_http_client(request),
        base_url=_agent_profiles_base_url(),
        headers=_agent_profiles_headers(request, user),
    )


def _validate_unique_agent_profile_names(profiles: list[AgentProfileInfo]) -> None:
    duplicates = sorted(
        name
        for name, count in Counter(profile.name for profile in profiles).items()
        if count > 1
    )
    if duplicates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Duplicate agent profile names found: " + ", ".join(duplicates),
        )


def _string_set_from_profile_list(
    value: list[str] | None, field_name: str, resolved_name: str
) -> set[str]:
    if value is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Agent profile `{resolved_name}` must explicitly set `{field_name}`",
        )
    return set(value)


def _validate_exact_allowlist(
    *,
    selected: set[str],
    allowed: list[str],
    resolved_name: str,
    label: str,
) -> None:
    allowed_set = set(allowed)
    missing = sorted(allowed_set - selected)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            (
                f"Agent profile `{resolved_name}` does not enable every {label} "
                f"allowed by this automation plugin: {', '.join(missing)}"
            ),
        )
    disallowed = sorted(selected - allowed_set)
    if disallowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            (
                f"Agent profile `{resolved_name}` enables {label} not allowed by "
                f"this automation plugin: {', '.join(disallowed)}"
            ),
        )


async def validate_agent_profile_allowlists_with_client(
    agent_profile_id: uuid.UUID | None,
    agent_profile_name: str | None,
    allowed_tools: list[str] | None,
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    allowed_mcp_server_refs: list[str] | None = None,
    allowed_secret_refs: list[str] | None = None,
    fetch_profile_detail: bool = False,
) -> tuple[uuid.UUID | None, AgentProfileBase | None]:
    """Reject missing/ambiguous profiles and profile fields outside allowlists."""
    if agent_profile_id is None and not agent_profile_name:
        return None, None

    profiles = await _list_agent_profiles_from_api(
        client, base_url=base_url, headers=headers
    )
    _validate_unique_agent_profile_names(profiles)
    resolved_id = agent_profile_id
    resolved_name = agent_profile_name
    if agent_profile_id is not None:
        agent_profile_id_str = str(agent_profile_id)
        resolved_name = next(
            (
                profile.name
                for profile in profiles
                if str(profile.id) == agent_profile_id_str
            ),
            None,
        )
        if resolved_name is None:
            raise HTTPException(422, f"Agent profile `{agent_profile_id}` not found")
    elif agent_profile_name:
        selected = next(
            (profile for profile in profiles if profile.name == agent_profile_name),
            None,
        )
        if selected is None:
            raise HTTPException(422, f"Agent profile `{agent_profile_name}` not found")
        if selected.id is None:
            raise HTTPException(422, f"Agent profile `{agent_profile_name}` has no id")
        resolved_id = uuid.UUID(str(selected.id))
    if not resolved_name or resolved_id is None:
        raise HTTPException(422, "Plugin automation requires an agent profile")

    needs_detail = (
        fetch_profile_detail
        or allowed_tools is not None
        or allowed_mcp_server_refs is not None
        or allowed_secret_refs is not None
    )
    if not needs_detail:
        return resolved_id, None

    try:
        resp = await client.get(
            f"{base_url}/api/agent-profiles/{quote(resolved_name, safe='')}",
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
        profile = _agent_profile_detail_from_response(resp)
    except (ValueError, TypeError, ValidationError) as exc:
        logger.warning("Agent profile detail lookup failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected response from OpenHands API for agent profile details",
        ) from exc

    if allowed_tools is not None:
        if not isinstance(profile, OpenHandsAgentProfile) or profile.tools is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                (
                    f"Agent profile `{resolved_name}` must explicitly enable the "
                    "tools allowed by this automation plugin: "
                    f"{', '.join(allowed_tools)}"
                ),
            )
        selected_tools = {tool.name for tool in profile.tools}
        _validate_exact_allowlist(
            selected=selected_tools,
            allowed=allowed_tools,
            resolved_name=resolved_name,
            label="tool",
        )

    if allowed_mcp_server_refs is not None:
        _validate_exact_allowlist(
            selected=_string_set_from_profile_list(
                profile.mcp_server_refs, "mcp_server_refs", resolved_name
            ),
            allowed=allowed_mcp_server_refs,
            resolved_name=resolved_name,
            label="MCP server ref",
        )

    if allowed_secret_refs is not None:
        _validate_exact_allowlist(
            selected=_string_set_from_profile_list(
                profile.secret_refs, "secret_refs", resolved_name
            ),
            allowed=allowed_secret_refs,
            resolved_name=resolved_name,
            label="secret ref",
        )

    return resolved_id, profile


async def validate_agent_profile_allowlists(
    agent_profile_id: uuid.UUID | None,
    agent_profile_name: str | None,
    allowed_tools: list[str] | None,
    request: Request,
    user: AuthenticatedUser,
    *,
    allowed_mcp_server_refs: list[str] | None = None,
    allowed_secret_refs: list[str] | None = None,
) -> uuid.UUID | None:
    resolved_id, _profile = await validate_agent_profile_allowlists_with_client(
        agent_profile_id,
        agent_profile_name,
        allowed_tools,
        get_http_client(request),
        base_url=_agent_profiles_base_url(),
        headers=_agent_profiles_headers(request, user),
        allowed_mcp_server_refs=allowed_mcp_server_refs,
        allowed_secret_refs=allowed_secret_refs,
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
        known = frozenset(
            str(profile.id) for profile in _agent_profiles_from_response(resp)
        )
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
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
