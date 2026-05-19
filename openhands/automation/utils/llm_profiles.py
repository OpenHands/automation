"""Helpers for resolving and validating LLM profile selections."""

from fastapi import HTTPException, status

from openhands.automation.auth import AuthenticatedUser


def validate_llm_profile_for_user(
    llm_profile: str | None, user: AuthenticatedUser
) -> None:
    """Validate a selected LLM profile against authenticated user metadata.

    Profile metadata is available only when the upstream auth response includes
    `llm_profiles`. If it is absent (for example, local mode or older upstream
    responses), runtime profile lookup remains the source of truth.
    """
    if not llm_profile or user.llm_profile_names is None:
        return

    if llm_profile not in user.llm_profile_names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"LLM profile `{llm_profile}` not found",
        )


def resolve_llm_profile_for_user(
    requested_profile: str | None, user: AuthenticatedUser
) -> str | None:
    """Resolve the profile name an automation should persist.

    Automations store LLM profile names, not raw LLM settings. If the request does
    not specify a profile, use the user's active profile at creation/update time.
    Older/local auth responses may not include profile metadata; in that case we
    leave the value unset so existing fallback behavior can preserve compatibility.
    """
    llm_profile = requested_profile or user.active_llm_profile_name
    validate_llm_profile_for_user(llm_profile, user)
    return llm_profile
