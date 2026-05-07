"""Local agent-server execution backend.

Uses a pre-configured local agent server instead of creating Cloud sandboxes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from automation.backends.base import ExecutionBackend, ExecutionContext
from automation.utils.agent_server import VerificationResult, verify_run_on_agent_server


if TYPE_CHECKING:
    from automation.models import AutomationRun

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

    def __init__(
        self,
        agent_server_url: str,
        api_key: str,
        run: AutomationRun,
        workspace_base: str = "/workspace",
    ):
        """Initialize the local agent-server backend for a specific run.

        Args:
            agent_server_url: URL of the local agent server
                (e.g., "http://localhost:3000")
            api_key: API key for authenticating with the agent server
            run: The automation run this backend will operate on
            workspace_base: Base workspace directory (default: /workspace)
        """
        self.agent_server_url = agent_server_url.rstrip("/")
        self.api_key = api_key
        self._run = run
        self.workspace_base = workspace_base

    @property
    def is_local_mode(self) -> bool:
        return True

    async def get_execution_context(
        self,
        client: httpx.AsyncClient,  # noqa: ARG002
    ) -> ExecutionContext:
        """Return the pre-configured agent server context.

        No sandbox creation needed — the agent server is already running.
        """
        logger.debug(
            "Using local agent server at %s",
            self.agent_server_url,
        )
        return ExecutionContext(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            sandbox_id=None,  # No sandbox in local mode
        )

    async def release_context(
        self,
        client: httpx.AsyncClient,  # noqa: ARG002
        ctx: ExecutionContext,  # noqa: ARG002
    ) -> None:
        """No-op — local agent server is persistent."""
        logger.debug("Local mode: skipping context release (persistent server)")

    async def get_api_key(self) -> str:
        """Return the pre-configured API key."""
        return self.api_key

    def build_env_vars(self) -> dict[str, str]:
        """Build local mode environment variables.

        Provides the env vars needed for local mode:
        - AGENT_SERVER_URL: URL of the local agent server
        - SESSION_API_KEY: API key for authenticating with the agent server
        - WORKSPACE_BASE: Base workspace directory for SDK operations
        """
        return {
            "AGENT_SERVER_URL": self.agent_server_url,
            "SESSION_API_KEY": self.api_key,
            "WORKSPACE_BASE": self.workspace_base,
        }

    async def verify_run(self, run_id: str) -> VerificationResult:
        """Verify run status by querying agent server directly."""
        return await verify_run_on_agent_server(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            run_id=run_id,
        )

    async def cleanup_after_verification(
        self,
        run_id: str,  # noqa: ARG002
    ) -> None:
        """No-op — local agent server is persistent."""
        logger.debug("Local mode: skipping cleanup (persistent server)")
