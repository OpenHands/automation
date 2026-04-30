"""Local agent-server execution backend.

Uses a pre-configured local agent server instead of creating Cloud sandboxes.
"""

import logging

import httpx

from automation.backends.base import ExecutionBackend, ExecutionContext


logger = logging.getLogger(__name__)


class LocalAgentServerBackend(ExecutionBackend):
    """Execution backend for local/self-hosted deployments.

    Uses a persistent, pre-configured agent server. No sandbox creation
    or cleanup is performed — the agent server is assumed to be running
    and managed externally.

    This is suitable for:
    - Local development
    - Self-hosted deployments
    - Single-tenant environments
    """

    def __init__(self, agent_server_url: str, api_key: str):
        """Initialize the local agent-server backend.

        Args:
            agent_server_url: URL of the local agent server
                (e.g., "http://localhost:3000")
            api_key: API key for authenticating with the agent server
        """
        self.agent_server_url = agent_server_url.rstrip("/")
        self.api_key = api_key

    @property
    def is_local_mode(self) -> bool:
        return True

    async def acquire(self, client: httpx.AsyncClient) -> ExecutionContext:  # noqa: ARG002
        """Return the pre-configured agent server context.

        No sandbox creation needed — the agent server is already running.
        """
        del client  # unused in local mode
        logger.debug(
            "Using local agent server at %s",
            self.agent_server_url,
        )
        return ExecutionContext(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            sandbox_id=None,  # No sandbox in local mode
        )

    async def release(self, client: httpx.AsyncClient, ctx: ExecutionContext) -> None:  # noqa: ARG002
        """No-op — local agent server is persistent."""
        del client, ctx  # unused in local mode
        logger.debug("Local mode: skipping sandbox cleanup (persistent server)")
