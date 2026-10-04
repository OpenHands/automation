"""Helpers for resolving and validating model profile selections."""

import logging
import uuid

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
