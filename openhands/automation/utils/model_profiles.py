"""Helpers for resolving and validating model profile selections."""

import uuid

import httpx
from fastapi import HTTPException, Request, status

from openhands.automation.auth import (
    AuthenticatedUser,
    get_http_client,
    upstream_auth_headers,
)


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


def validate_agent_profile_selection(
    agent_profile_id: uuid.UUID | None, model: str | None
) -> None:
    """An agent profile owns its model and is resolved by the configured server."""
    if agent_profile_id is None:
        return
    from openhands.automation.config import get_config

    if not get_config().service.is_local_mode:
        raise HTTPException(422, "Agent profiles require a configured Agent Server")
    if model:
        raise HTTPException(422, "An agent profile already specifies the model")


async def validate_agent_profile(
    agent_profile_id: uuid.UUID | None, model: str | None, request: Request
) -> None:
    """Validate an agent profile selected through the API.

    A local deployment hands the id to its Agent Server. In cloud mode the
    profiles belong to the caller's organization in the OpenHands app server,
    which starts a conversation with default settings rather than fail on an id
    it does not know, so the id is checked against that organization here.
    """
    if agent_profile_id is None:
        return
    from openhands.automation.config import get_config

    settings = get_config().service
    if settings.is_local_mode:
        validate_agent_profile_selection(agent_profile_id, model)
        return
    if model:
        raise HTTPException(422, "An agent profile already specifies the model")

    try:
        resp = await get_http_client(request).get(
            f"{settings.openhands_api_base_url}/api/agent-profiles",
            headers=upstream_auth_headers(request),
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Failed to reach OpenHands API for agent profiles",
        ) from exc
    if resp.status_code != 200:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Unexpected response from OpenHands API for agent profiles",
        )
    known = {str(profile.get("id")) for profile in resp.json().get("profiles", [])}
    if str(agent_profile_id) not in known:
        raise HTTPException(422, f"Agent profile `{agent_profile_id}` not found")
