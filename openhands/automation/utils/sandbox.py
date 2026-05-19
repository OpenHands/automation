"""Sandbox verification and cleanup utilities.

Provides functions to verify automation run status by querying the sandbox's
bash command history, and to clean up sandboxes after runs complete.

For Cloud mode only — uses sandbox discovery to find agent server URL.
For local mode, use utils/agent_server.py directly.
"""

import logging

import httpx

from openhands.automation.utils.agent_server import (
    BashCommandResult,
    VerificationResult,
    get_last_bash_command_result,
)
from openhands.automation.utils.log_context import log_extra


# Re-export for backward compatibility
__all__ = [
    "BashCommandResult",
    "VerificationResult",
    "get_last_bash_command_result",
    "get_sandbox_agent_url",
    "delete_sandbox",
    "cleanup_sandbox",
    "verify_run_status",
]

logger = logging.getLogger(__name__)


async def get_sandbox_agent_url(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    sandbox_id: str,
) -> tuple[str, str] | None:
    """Get the agent server URL and session key for a sandbox.

    Returns (agent_url, session_key) if the sandbox is running with an agent server,
    or None if the sandbox is not available.

    Cloud mode only — discovers agent server via sandbox API.
    """
    try:
        resp = await client.get(
            f"{api_url}/api/v1/sandboxes",
            params={"id": sandbox_id},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return None

        sandbox = items[0]
        if sandbox.get("status") != "RUNNING":
            return None

        for url_info in sandbox.get("exposed_urls") or []:
            if url_info.get("name") == "AGENT_SERVER":
                return url_info["url"].rstrip("/"), sandbox.get("session_api_key", "")
        return None
    except Exception as e:
        logger.warning("Failed to get sandbox %s: %s", sandbox_id, e)
        return None


async def delete_sandbox(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    sandbox_id: str,
) -> bool:
    """Delete a sandbox using an existing client. Returns True if successful."""
    try:
        resp = await client.delete(
            f"{api_url}/api/v1/sandboxes/{sandbox_id}",
            params={"sandbox_id": sandbox_id},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code >= 300:
            logger.warning("Delete sandbox %s failed: %s", sandbox_id, resp.text)
            return False
        return True
    except Exception as e:
        logger.warning("Error deleting sandbox %s: %s", sandbox_id, e)
        return False


async def cleanup_sandbox(
    api_url: str,
    api_key: str,
    sandbox_id: str,
    run_id: str | None = None,
) -> bool:
    """Delete a sandbox (best-effort, creates its own HTTP client).

    This is the main entry point for sandbox cleanup. Use this from routes
    and background tasks.

    Args:
        api_url: OpenHands API URL
        api_key: API key for authentication
        sandbox_id: The sandbox to delete
        run_id: Optional run ID for logging

    Returns:
        True if sandbox was deleted successfully
    """
    api_url = api_url.rstrip("/")
    extra = log_extra(run_id=run_id, sandbox_id=sandbox_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            deleted = await delete_sandbox(client, api_url, api_key, sandbox_id)
            if deleted:
                logger.info("Sandbox deleted", extra=extra)
            else:
                logger.warning("Failed to delete sandbox", extra=extra)
            return deleted
    except Exception:
        logger.exception("Error deleting sandbox", extra=extra)
        return False


async def verify_run_status(
    api_url: str,
    api_key: str,
    sandbox_id: str,
    keep_alive: bool = False,
    run_id: str | None = None,
    bash_command_id: str | None = None,
) -> VerificationResult:
    """Verify an automation run's status by querying its sandbox.

    Connects to the sandbox, queries the last bash command's exit code,
    and optionally deletes the sandbox.

    Args:
        api_url: OpenHands API URL
        api_key: API key for authentication
        sandbox_id: The sandbox to query
        keep_alive: If True, don't delete the sandbox after verification
        run_id: Optional run ID for logging
        bash_command_id: Optional BashCommand id (hex) recorded for this
            run; scopes the BashOutput lookup to this specific command.
            In cloud mode each run owns its sandbox so contamination is
            unlikely, but scoping is still safer when the agent inside
            the sandbox runs other bash commands during the run.

    Returns:
        VerificationResult with the verification outcome
    """
    api_url = api_url.rstrip("/")
    extra = log_extra(run_id=run_id, sandbox_id=sandbox_id)

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get sandbox agent URL
        result = await get_sandbox_agent_url(client, api_url, api_key, sandbox_id)
        if result is None:
            logger.info("Sandbox not available for verification", extra=extra)
            return VerificationResult(
                verified=False,
                error="Sandbox not available",
            )

        agent_url, session_key = result
        logger.info("Connected to sandbox for verification", extra=extra)

        # Get last bash command result, scoped to this run's command if known
        bash_result = await get_last_bash_command_result(
            client, agent_url, session_key, command_id=bash_command_id
        )

        if not bash_result.found:
            logger.warning(
                "Could not find bash command result: %s",
                bash_result.error,
                extra=extra,
            )
            # Still try to clean up if needed
            if not keep_alive:
                await delete_sandbox(client, api_url, api_key, sandbox_id)
            return VerificationResult(
                verified=False,
                error=bash_result.error,
            )

        if bash_result.exit_code is None:
            logger.info("Bash command still running", extra=extra)
            return VerificationResult(
                verified=False,
                error="Command still running",
            )

        success = bash_result.exit_code == 0
        logger.info(
            "Verified run status: exit_code=%s, success=%s",
            bash_result.exit_code,
            success,
            extra=extra,
        )

        # Clean up sandbox if not keeping alive
        if not keep_alive:
            logger.info("Deleting sandbox", extra=extra)
            await delete_sandbox(client, api_url, api_key, sandbox_id)

        return VerificationResult(
            verified=True,
            success=success,
            exit_code=bash_result.exit_code,
            stdout=bash_result.stdout,
            stderr=bash_result.stderr,
        )
