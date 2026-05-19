"""Shared LLM profile helpers for generated automation preset runtimes."""

from urllib.parse import quote

import httpx


def _load_named_llm_profile_config(
    profile_name: str,
    *,
    is_local_mode: bool,
    agent_server_url: str,
    api_url: str,
    api_key: str,
    session_key: str,
) -> dict[str, object]:
    """Resolve an LLM profile using the same profile APIs as Agent Canvas."""
    if is_local_mode:
        headers = {"X-Expose-Secrets": "plaintext"}
        if session_key:
            headers["X-Session-API-Key"] = session_key
        response = httpx.get(
            f"{agent_server_url}/api/profiles/{quote(profile_name, safe='')}",
            headers=headers,
            timeout=30,
        )
        if response.status_code == 404:
            raise FileNotFoundError(f"LLM profile `{profile_name}` not found")
        response.raise_for_status()
        config = response.json().get("config")
    else:
        response = httpx.get(
            f"{api_url}/api/v1/users/me",
            params={"expose_secrets": "true"},
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Session-API-Key": session_key,
            },
            timeout=30,
        )
        response.raise_for_status()
        llm_profiles = response.json().get("llm_profiles") or {}
        profiles = (
            llm_profiles.get("profiles") if isinstance(llm_profiles, dict) else None
        )
        config = profiles.get(profile_name) if isinstance(profiles, dict) else None
        if config is None:
            available = sorted(profiles) if isinstance(profiles, dict) else []
            raise FileNotFoundError(
                f"LLM profile `{profile_name}` not found. "
                f"Available profiles: {', '.join(available) or 'none'}"
            )

    if not isinstance(config, dict):
        raise ValueError(f"LLM profile `{profile_name}` has invalid config")
    return config


def get_automation_llm(
    workspace,
    profile_name: str | None,
    *,
    is_local_mode: bool,
    agent_server_url: str,
    api_url: str,
    api_key: str,
    session_key: str,
):
    if not profile_name:
        # Legacy/local fallback: new automations persist an explicit profile name,
        # but older rows may not have one if auth metadata was unavailable.
        return workspace.get_llm()

    from openhands.sdk.llm.llm import LLM

    return LLM(
        **_load_named_llm_profile_config(
            profile_name,
            is_local_mode=is_local_mode,
            agent_server_url=agent_server_url,
            api_url=api_url,
            api_key=api_key,
            session_key=session_key,
        )
    )
