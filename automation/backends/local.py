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
        cloud_api_url: str | None = None,
    ):
        """Initialize the local agent-server backend.

        Args:
            agent_server_url: URL of the local agent server
                (e.g., "http://localhost:3000")
            api_key: API key for authenticating with the agent server
            cloud_api_url: Optional Cloud API URL for LLM/secrets access
        """
        self.agent_server_url = agent_server_url.rstrip("/")
        self.api_key = api_key
        self.cloud_api_url = cloud_api_url

    @property
    def is_local_mode(self) -> bool:
        return True

    async def acquire(
        self,
        client: httpx.AsyncClient,  # noqa: ARG002
    ) -> ExecutionContext:
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

    async def release(
        self,
        client: httpx.AsyncClient,  # noqa: ARG002
        ctx: ExecutionContext,  # noqa: ARG002
    ) -> None:
        """No-op — local agent server is persistent."""
        del client, ctx  # unused in local mode
        logger.debug("Local mode: skipping sandbox cleanup (persistent server)")

    async def get_api_key(
        self,
        run: AutomationRun,  # noqa: ARG002
    ) -> str:
        """Return the pre-configured API key."""
        return self.api_key

    def build_env_vars(
        self,
        api_key: str,  # noqa: ARG002
    ) -> dict[str, str]:
        """Build local mode environment variables."""
        env_vars = {
            "AGENT_SERVER_URL": self.agent_server_url,
        }
        # Optionally include Cloud API URL for LLM/secrets access
        if self.cloud_api_url:
            env_vars["OPENHANDS_CLOUD_API_URL"] = self.cloud_api_url
        return env_vars

    async def verify_run(
        self,
        run: AutomationRun,  # noqa: ARG002
        run_id: str,
    ) -> VerificationResult:
        """Verify run status by querying agent server directly."""
        return await verify_run_on_agent_server(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            run_id=run_id,
        )

    async def cleanup_after_verification(
        self,
        run: AutomationRun,  # noqa: ARG002
        run_id: str,  # noqa: ARG002
    ) -> None:
        """No-op — local agent server is persistent."""
        logger.debug("Local mode: skipping cleanup (persistent server)")
