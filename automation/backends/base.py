"""Base classes for execution backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx


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

    Execution backends handle the lifecycle of acquiring and releasing
    execution contexts. The execution logic (upload, bash commands) is
    shared across backends.
    """

    @abstractmethod
    async def acquire(self, client: httpx.AsyncClient) -> ExecutionContext:
        """Acquire an execution context (agent server URL + credentials).

        For Cloud mode: Creates a sandbox, waits for it to be RUNNING,
        and extracts the agent server URL from exposed_urls.

        For Local mode: Returns the pre-configured agent server URL.

        Args:
            client: HTTP client for making requests

        Returns:
            ExecutionContext with agent_url and session_key

        Raises:
            RuntimeError: If acquisition fails
            TimeoutError: If sandbox doesn't become ready in time (Cloud mode)
        """

    @abstractmethod
    async def release(self, client: httpx.AsyncClient, ctx: ExecutionContext) -> None:
        """Release the execution context (cleanup).

        For Cloud mode: Deletes the sandbox.
        For Local mode: No-op (persistent server).

        Args:
            client: HTTP client for making requests
            ctx: The execution context to release
        """

    @property
    @abstractmethod
    def is_local_mode(self) -> bool:
        """Whether this backend operates in local mode."""
