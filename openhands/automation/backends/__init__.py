"""Execution backends for automation runs.

Provides pluggable backends for getting and releasing execution contexts:
- CloudSandboxBackend: Creates fresh Cloud sandboxes per run (default)
- LocalAgentServerBackend: Uses a pre-configured local agent server

Usage:
    from openhands.automation.backends import get_backend

    backend = get_backend(run)  # Returns backend for this run
    ctx = await backend.get_execution_context(client)
    try:
        # Use ctx.agent_url and ctx.session_key
        ...
    finally:
        await backend.release_context(client, ctx)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openhands.automation.backends.base import ExecutionBackend, ExecutionContext
from openhands.automation.backends.cloud import CloudSandboxBackend
from openhands.automation.backends.local import LocalAgentServerBackend


if TYPE_CHECKING:
    from openhands.automation.models import AutomationRun


def get_backend(run: AutomationRun) -> ExecutionBackend:
    """Get the appropriate execution backend for an automation run.

    Args:
        run: The automation run this backend will operate on.

    Returns:
        ExecutionBackend: Either CloudSandboxBackend or LocalAgentServerBackend
    """
    from openhands.automation.config import get_config

    config = get_config()
    settings = config.service

    if settings.is_local_mode:
        return LocalAgentServerBackend(
            agent_server_url=settings.agent_server_url,
            api_key=settings.agent_server_api_key,
            run=run,
            workspace_base=settings.workspace_base,
            callback_api_key=settings.local_api_key,
        )
    else:
        return CloudSandboxBackend(
            api_url=settings.openhands_api_base_url,
            run=run,
        )


__all__ = [
    "ExecutionBackend",
    "ExecutionContext",
    "CloudSandboxBackend",
    "LocalAgentServerBackend",
    "get_backend",
]
