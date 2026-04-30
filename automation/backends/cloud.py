"""Cloud sandbox execution backend.

Creates a fresh Cloud sandbox for each automation run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from automation.backends.base import ExecutionBackend, ExecutionContext
from automation.config import get_config
from automation.utils.api_key import get_api_key_for_automation_run
from automation.utils.sandbox import cleanup_sandbox, delete_sandbox, verify_run_status


if TYPE_CHECKING:
    from automation.models import AutomationRun
    from automation.utils.agent_server import VerificationResult

logger = logging.getLogger(__name__)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Check if exception is a 429 rate limit error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return False


class CloudSandboxBackend(ExecutionBackend):
    """Execution backend that creates Cloud sandboxes per run.

    This is the default backend for OpenHands Cloud deployments.
    Each automation run gets a fresh, isolated sandbox.
    """

    def __init__(self, api_url: str, api_key: str):
        """Initialize the Cloud sandbox backend.

        Args:
            api_url: OpenHands Cloud API URL (e.g., "https://app.all-hands.dev")
            api_key: API key for authenticating with the Cloud API
        """
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

        # Load sandbox config for retry/timeout settings
        sandbox_config = get_config().sandbox
        self._ready_timeout = sandbox_config.sandbox_ready_timeout
        self._poll_interval = sandbox_config.sandbox_poll_interval

        # Configure retry decorator for rate limiting
        self._retry = retry(
            retry=retry_if_exception(_is_rate_limit_error),
            stop=stop_after_attempt(sandbox_config.rate_limit_max_retries),
            wait=wait_exponential(
                min=sandbox_config.rate_limit_min_wait,
                max=sandbox_config.rate_limit_max_wait,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

    @property
    def is_local_mode(self) -> bool:
        return False

    async def acquire(self, client: httpx.AsyncClient) -> ExecutionContext:
        """Create a sandbox and wait for it to be ready."""
        sandbox_id, session_key, agent_url = await self._create_and_wait(client)
        return ExecutionContext(
            agent_url=agent_url,
            session_key=session_key,
            sandbox_id=sandbox_id,
            api_url=self.api_url,
            api_key=self.api_key,
        )

    async def release(self, client: httpx.AsyncClient, ctx: ExecutionContext) -> None:
        """Delete the sandbox."""
        if ctx.sandbox_id and ctx.api_url and ctx.api_key:
            await delete_sandbox(client, ctx.api_url, ctx.api_key, ctx.sandbox_id)

    async def get_api_key(self, run: AutomationRun) -> str:
        """Mint a per-user API key via the service key."""
        return await get_api_key_for_automation_run(run)

    def build_env_vars(self, api_key: str) -> dict[str, str]:
        """Build Cloud mode environment variables."""
        return {
            "OPENHANDS_API_KEY": api_key,
            "OPENHANDS_CLOUD_API_URL": self.api_url,
        }

    async def verify_run(
        self,
        run: AutomationRun,
        run_id: str,
    ) -> VerificationResult:
        """Verify run status via sandbox discovery."""
        sandbox_id = run.sandbox_id
        if not sandbox_id:
            from automation.utils.agent_server import VerificationResult

            return VerificationResult(
                verified=False,
                error="No sandbox_id available for verification",
            )

        # Get API key for sandbox access
        api_key = await get_api_key_for_automation_run(run)

        return await verify_run_status(
            api_url=self.api_url,
            api_key=api_key,
            sandbox_id=sandbox_id,
            keep_alive=run.keep_alive,
            run_id=run_id,
        )

    async def cleanup_after_verification(
        self,
        run: AutomationRun,
        run_id: str,
    ) -> None:
        """Clean up sandbox after verification failure."""
        sandbox_id = run.sandbox_id
        if not run.keep_alive and sandbox_id:
            api_key = await get_api_key_for_automation_run(run)
            await cleanup_sandbox(
                api_url=self.api_url,
                api_key=api_key,
                sandbox_id=sandbox_id,
                run_id=run_id,
            )

    async def _create_and_wait(
        self,
        client: httpx.AsyncClient,
        ready_timeout: float | None = None,
    ) -> tuple[str, str, str]:
        """Create a sandbox and poll until RUNNING.

        Returns (sandbox_id, session_api_key, agent_server_url).
        """
        if ready_timeout is None:
            ready_timeout = self._ready_timeout

        headers = {"Authorization": f"Bearer {self.api_key}"}
        sandbox_id = await self._create_sandbox(client, headers)

        elapsed = 0.0
        while elapsed < ready_timeout:
            sb = await self._poll_sandbox(client, sandbox_id, headers)
            status = sb.get("status", "UNKNOWN")

            if status == "RUNNING":
                result = self._find_agent_server_url(sb)
                if result is None:
                    raise RuntimeError(f"No AGENT_SERVER URL in sandbox {sandbox_id}")
                agent_url, session_key = result
                return sandbox_id, session_key, agent_url

            if status in ("ERROR", "MISSING"):
                error_code = sb.get("error_code", "")
                error_message = sb.get("error_message", "")
                error_detail = f"status={status}"
                if error_code:
                    error_detail += f", error_code={error_code}"
                if error_message:
                    error_detail += f", error_message={error_message}"
                raise RuntimeError(f"Sandbox {sandbox_id} failed: {error_detail}")

            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval

        raise TimeoutError(f"Sandbox {sandbox_id} not ready after {ready_timeout}s")

    async def _create_sandbox(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> str:
        """Create a sandbox and return its ID."""

        @self._retry
        async def _do_create():
            resp = await client.post(
                f"{self.api_url}/api/v1/sandboxes", headers=headers
            )
            resp.raise_for_status()
            return resp.json()["id"]

        return await _do_create()

    async def _poll_sandbox(
        self, client: httpx.AsyncClient, sandbox_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        """Poll sandbox status."""

        @self._retry
        async def _do_poll():
            resp = await client.get(
                f"{self.api_url}/api/v1/sandboxes",
                params={"id": sandbox_id},
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json()
            if not items:
                raise RuntimeError(f"Sandbox {sandbox_id} disappeared")
            return items[0]

        return await _do_poll()

    @staticmethod
    def _find_agent_server_url(sandbox: dict) -> tuple[str, str] | None:
        """Extract (agent_url, session_key) from sandbox response."""
        for url_info in sandbox.get("exposed_urls") or []:
            if url_info.get("name") == "AGENT_SERVER":
                return url_info["url"].rstrip("/"), sandbox.get("session_api_key", "")
        return None
