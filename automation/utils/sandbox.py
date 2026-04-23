"""Sandbox verification and cleanup utilities.

Provides functions to verify automation run status by querying the sandbox's
bash command history, and to clean up sandboxes after runs complete.
"""

import logging
from typing import Any

import httpx
from pydantic.dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BashCommandResult:
    """Result of querying a sandbox for the last bash command."""

    found: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


def _log_extra(
    run_id: str | None = None, sandbox_id: str | None = None
) -> dict[str, Any]:
    """Build extra dict for structured logging."""
    extra: dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if sandbox_id:
        extra["sandbox_id"] = sandbox_id
    return extra


async def get_sandbox_agent_url(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    sandbox_id: str,
) -> tuple[str, str] | None:
    """Get the agent server URL and session key for a sandbox.

    Returns (agent_url, session_key) if the sandbox is running with an agent server,
    or None if the sandbox is not available.
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


async def get_last_bash_command_result(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
) -> BashCommandResult:
    """Query the sandbox for the last bash command's result.

    Returns the exit code and output of the most recent bash command.
    """
    try:
        # Search for the most recent BashOutput event
        resp = await client.get(
            f"{agent_url}/api/bash/bash_events/search",
            params={
                "kind__eq": "BashOutput",
                "sort_order": "TIMESTAMP_DESC",
                "limit": 1,
            },
            headers={"X-Session-API-Key": session_key},
            timeout=30.0,
        )
        resp.raise_for_status()
        page = resp.json()

        items = page.get("items", [])
        if not items:
            return BashCommandResult(found=False, error="No bash output found")

        output = items[0]
        exit_code = output.get("exit_code")

        # If exit_code is None, the command is still running
        if exit_code is None:
            return BashCommandResult(
                found=True,
                exit_code=None,
                error="Command still running",
            )

        return BashCommandResult(
            found=True,
            exit_code=exit_code,
            stdout=output.get("stdout") or "",
            stderr=output.get("stderr") or "",
        )
    except Exception as e:
        logger.warning("Failed to get bash command result: %s", e)
        return BashCommandResult(found=False, error=str(e))


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
    extra = _log_extra(run_id=run_id, sandbox_id=sandbox_id)

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


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying an automation run's sandbox status."""

    verified: bool
    success: bool | None = None  # None if not verified
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


async def verify_run_status(
    api_url: str,
    api_key: str,
    sandbox_id: str,
    keep_alive: bool = False,
    run_id: str | None = None,
    agent_server_url: str | None = None,
    agent_server_api_key: str | None = None,
) -> VerificationResult:
    """Verify an automation run's status by querying its runtime.

    **Cloud sandbox mode** (``agent_server_url`` is ``None``):
    Discovers the agent-server inside the sandbox, queries bash history,
    and optionally deletes the sandbox.

    **Agent-server mode** (``agent_server_url`` is set):
    Queries the configured agent-server directly.  No sandbox cleanup.

    Args:
        api_url: OpenHands API URL
        api_key: API key for authentication
        sandbox_id: The sandbox to query (Cloud mode)
        keep_alive: If True, don't delete the sandbox after verification
        run_id: Optional run ID for logging
        agent_server_url: Direct agent-server URL (agent-server mode)
        agent_server_api_key: API key for the agent-server

    Returns:
        VerificationResult with the verification outcome
    """
    api_url = api_url.rstrip("/")
    extra = _log_extra(run_id=run_id, sandbox_id=sandbox_id)
    is_agent_server_mode = agent_server_url is not None

    async with httpx.AsyncClient(timeout=60.0) as client:
        # --- Resolve agent-server connection ---
        if is_agent_server_mode:
            agent_url = agent_server_url.rstrip("/")
            session_key = agent_server_api_key or ""
            logger.info("Using configured agent-server for verification", extra=extra)
        else:
            result = await get_sandbox_agent_url(client, api_url, api_key, sandbox_id)
            if result is None:
                logger.info("Sandbox not available for verification", extra=extra)
                return VerificationResult(
                    verified=False,
                    error="Sandbox not available",
                )
            agent_url, session_key = result
            logger.info("Connected to sandbox for verification", extra=extra)

        # --- Query bash history (shared) ---
        bash_result = await get_last_bash_command_result(client, agent_url, session_key)

        if not bash_result.found:
            logger.warning(
                "Could not find bash command result: %s",
                bash_result.error,
                extra=extra,
            )
            if not is_agent_server_mode and not keep_alive:
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

        # Only clean up in Cloud sandbox mode
        if not is_agent_server_mode and not keep_alive:
            logger.info("Deleting sandbox", extra=extra)
            await delete_sandbox(client, api_url, api_key, sandbox_id)

        return VerificationResult(
            verified=True,
            success=success,
            exit_code=bash_result.exit_code,
            stdout=bash_result.stdout,
            stderr=bash_result.stderr,
        )
