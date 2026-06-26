"""Base classes for execution backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx


if TYPE_CHECKING:
    from openhands.automation.utils.agent_server import VerificationResult


@dataclass
class ExecutionContext:
    """Context for executing commands on an agent server.

    Attributes:
        agent_url: Base URL of the agent server (e.g., "http://localhost:3000")
        session_key: API key for authenticating with the agent server
        sandbox_id: Sandbox ID (Cloud mode only, None for local mode)
        api_url: Cloud API URL (Cloud mode only, needed for sandbox cleanup)
        api_key: Cloud API key (Cloud mode only, needed for sandbox cleanup)
    """

    agent_url: str
    session_key: str
    sandbox_id: str | None = None
    api_url: str | None = None
    api_key: str | None = None


class ExecutionBackend(ABC):
    """Abstract base class for execution backends.

    Execution backends encapsulate all mode-specific behavior:
    - Sandbox/agent server lifecycle (acquire/release)
    - API key acquisition
    - Environment variable injection
    - Run verification and cleanup

    This keeps dispatcher and watchdog mode-agnostic.
    """

    @abstractmethod
    async def get_execution_context(
        self,
        client: httpx.AsyncClient,
    ) -> ExecutionContext:
        """Get the execution context (agent server URL + credentials).

        For Cloud mode: Creates a sandbox, waits for it to be RUNNING,
        and extracts the agent server URL from exposed_urls.

        For Local mode: Returns the pre-configured agent server URL.

        Args:
            client: HTTP client for making requests

        Returns:
            ExecutionContext with agent_url and session_key

        Raises:
            RuntimeError: If context cannot be obtained
            TimeoutError: If sandbox doesn't become ready in time (Cloud mode)
        """

    @abstractmethod
    async def release_context(
        self, client: httpx.AsyncClient, ctx: ExecutionContext
    ) -> None:
        """Release the execution context (cleanup).

        For Cloud mode: Deletes the sandbox.
        For Local mode: No-op (persistent server).

        Args:
            client: HTTP client for making requests
            ctx: The execution context to release
        """

    @abstractmethod
    async def get_api_key(self) -> str:
        """Get the API key for executing an automation run.

        For Cloud mode: Mints/returns the per-user API key.
        For Local mode: Returns the pre-configured API key.

        Returns:
            API key string
        """

    @abstractmethod
    def build_env_vars(self) -> dict[str, str]:
        """Build environment variables to inject into the execution environment.

        For Cloud mode: OPENHANDS_API_KEY, OPENHANDS_CLOUD_API_URL
        For Local mode: AGENT_SERVER_URL, SESSION_API_KEY

        Returns:
            Dictionary of environment variable name -> value
        """

    @abstractmethod
    async def verify_run(self, run_id: str) -> "VerificationResult":
        """Verify the status of a running automation.

        For Cloud mode: Discovers sandbox, queries agent server, cleans up.
        For Local mode: Queries agent server directly, no cleanup.

        Args:
            run_id: Run ID string for logging

        Returns:
            VerificationResult with verification outcome
        """

    @abstractmethod
    async def cleanup_after_verification(self, run_id: str) -> None:
        """Clean up resources after verification fails.

        For Cloud mode: Deletes the sandbox when called by policy-aware callers.
        For Local mode: No-op (persistent server).

        Args:
            run_id: Run ID string for logging
        """

    @property
    @abstractmethod
    def is_local_mode(self) -> bool:
        """Whether this backend operates in local mode."""

    @abstractmethod
    def get_work_dir(self, run_id: str) -> str:
        """Get the working directory for tarball extraction and execution.

        For Cloud mode: Returns /workspace/project (container filesystem).
        For Local mode: Returns {workspace_base}/automation-runs/{run_id}/

        Args:
            run_id: The automation run ID (used for isolation in local mode)

        Returns:
            Absolute path to the working directory
        """
