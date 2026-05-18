"""Local agent-server execution backend.

Uses a pre-configured local agent server instead of creating Cloud sandboxes.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx

from openhands.automation.backends.base import ExecutionBackend, ExecutionContext
from openhands.automation.utils.agent_server import (
    VerificationResult,
    verify_run_on_agent_server,
)


if TYPE_CHECKING:
    from openhands.automation.models import AutomationRun

logger = logging.getLogger(__name__)

# Default workspace base for local mode when running natively (not in container).
# Used as fallback when workspace_base is not explicitly configured.
#
# Why ~/.openhands/workspaces (not /workspace)?
# - Native local mode (macOS/Linux) runs outside containers; /workspace may not exist
# - ~/.openhands/workspaces follows the SDK convention for local development
# - Containerized local mode should explicitly set WORKSPACE_BASE=/workspace
#
# Note: The preset scripts (sdk_main.py) use the same fallback logic but have
# /workspace as their default because they run inside containers where this
# path is guaranteed to exist. The backend's WORKSPACE_BASE env var always
# overrides the preset's default, so the effective path is consistent.
DEFAULT_LOCAL_WORKSPACE_BASE = "~/.openhands/workspaces"


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
        workspace_base: str | None = None,
        callback_api_key: str | None = None,
    ):
        """Initialize the local agent-server backend for a specific run.

        Args:
            agent_server_url: URL of the local agent server
                (e.g., "http://localhost:3000")
            api_key: API key for authenticating with the agent server
            run: The automation run this backend will operate on
            workspace_base: Base workspace directory. If None, defaults to
                ~/.openhands/workspaces (suitable for native local mode).
            callback_api_key: API key for authenticating completion callbacks
                to the automation service (local_api_key from config). If None,
                callbacks will be sent without authentication.
        """
        self.agent_server_url = agent_server_url.rstrip("/")
        self.api_key = api_key
        self._run = run
        self.workspace_base = workspace_base
        self.callback_api_key = callback_api_key

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
        - WORKSPACE_BASE: Run-isolated workspace directory for SDK operations
        - AUTOMATION_CALLBACK_API_KEY: API key for callback auth to automation service

        The workspace is isolated per-run to avoid conflicts between concurrent
        automations. Each run gets its own directory under the base workspace.

        Note: AUTOMATION_CALLBACK_URL and AUTOMATION_RUN_ID are added by the
        dispatcher after calling this method.
        """
        # Use run-specific workspace directory for isolation
        run_workspace = self.get_work_dir(str(self._run.id))
        env_vars = {
            "AGENT_SERVER_URL": self.agent_server_url,
            "SESSION_API_KEY": self.api_key,
            "WORKSPACE_BASE": run_workspace,
        }
        # Add callback API key for RemoteWorkspace completion callback auth.
        # This is the automation service's local_api_key, NOT the agent server key.
        #
        # Note: AUTOMATION_CALLBACK_API_KEY support was added in SDK PR #3110.
        # Earlier SDK versions will ignore this env var and send callbacks without
        # authentication, which is acceptable for local-only deployments but may
        # fail if the automation service requires auth.
        #
        # See: https://github.com/All-Hands-AI/openhands-software-agent-sdk/pull/3110
        if self.callback_api_key:
            env_vars["AUTOMATION_CALLBACK_API_KEY"] = self.callback_api_key
        return env_vars

    async def verify_run(self, run_id: str) -> VerificationResult:
        """Verify run status by querying agent server directly.

        The stored ``bash_command_id`` (recorded right after dispatch) is
        forwarded so the verifier reads BashOutput from *this run's* bash
        chain only. Without it, the verifier samples the most recent
        BashOutput on the agent server — which on a shared dev/local
        server can easily belong to another run or to the agent's own
        TerminalTool, producing misleading error_detail values like
        "fatal: not a git repository" attributed to a run that never
        ran a git command.
        """
        return await verify_run_on_agent_server(
            agent_url=self.agent_server_url,
            session_key=self.api_key,
            run_id=run_id,
            bash_command_id=self._run.bash_command_id,
        )

    async def cleanup_after_verification(
        self,
        run_id: str,  # noqa: ARG002
    ) -> None:
        """No-op — local agent server is persistent."""
        logger.debug("Local mode: skipping cleanup (persistent server)")

    def get_work_dir(self, run_id: str) -> str:
        """Get an isolated working directory for this run.

        In local mode, each run gets its own directory under workspace_base
        to avoid conflicts between concurrent runs.

        Returns:
            Path like ~/.openhands/workspaces/automation-runs/{run_id}/
        """
        # Use configured workspace_base or default
        base = self.workspace_base or DEFAULT_LOCAL_WORKSPACE_BASE
        # Expand ~ to home directory
        base = os.path.expanduser(base)
        work_dir = os.path.join(base, "automation-runs", run_id)
        logger.debug(f"Local mode work directory: {work_dir}")
        return work_dir
