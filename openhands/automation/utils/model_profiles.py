"""Helpers for resolving and validating model profile selections."""

from fastapi import HTTPException, status

from openhands.automation.auth import AuthenticatedUser


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
