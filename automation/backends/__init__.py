"""Execution backends for automation runs.

Provides pluggable backends for acquiring and releasing execution contexts:
- CloudSandboxBackend: Creates fresh Cloud sandboxes per run (default)
- LocalAgentServerBackend: Uses a pre-configured local agent server

Usage:
    from automation.backends import get_backend

    backend = get_backend()  # Returns appropriate backend based on config
    ctx = await backend.acquire(client)
    try:
        # Use ctx.agent_url and ctx.session_key
        ...
    finally:
        await backend.release(client, ctx)
"""

from automation.backends.base import ExecutionBackend, ExecutionContext
from automation.backends.cloud import CloudSandboxBackend
from automation.backends.local import LocalAgentServerBackend


def get_backend(api_key: str | None = None) -> ExecutionBackend:
    """Get the appropriate execution backend based on configuration.

    Args:
        api_key: API key for Cloud mode (required if not in local mode).
            In Cloud mode, this is the per-user API key for sandbox creation.
            In local mode, this is ignored (config-level key is used).

    Returns:
        ExecutionBackend: Either CloudSandboxBackend or LocalAgentServerBackend

    Raises:
        ValueError: If api_key is required but not provided
    """
    from automation.config import get_config

    config = get_config()
    settings = config.service

    if settings.is_local_mode:
        return LocalAgentServerBackend(
            agent_server_url=settings.agent_server_url,
            api_key=settings.agent_server_api_key,
            cloud_api_url=settings.openhands_api_base_url or None,
            llm_model=settings.llm_model or None,
            llm_api_key=settings.llm_api_key or None,
            llm_base_url=settings.llm_base_url or None,
        )
    else:
        if not api_key:
            raise ValueError("api_key is required for Cloud mode")
        return CloudSandboxBackend(
            api_url=settings.openhands_api_base_url,
            api_key=api_key,
        )


__all__ = [
    "ExecutionBackend",
    "ExecutionContext",
    "CloudSandboxBackend",
    "LocalAgentServerBackend",
    "get_backend",
]
