"""Agent server utilities for verifying run status.

Provides functions to query an agent server's bash command history to verify
automation run status. These functions work with both Cloud sandboxes and
local agent servers.
"""

import logging

import httpx
from pydantic.dataclasses import dataclass

from automation.utils.log_context import log_extra


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BashCommandResult:
    """Result of querying an agent server for the last bash command."""

    found: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


async def get_last_bash_command_result(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
) -> BashCommandResult:
    """Query the agent server for the last bash command's result.

    Returns the exit code and output of the most recent bash command.
    Works with both Cloud sandboxes and local agent servers.

    Args:
        client: HTTP client
        agent_url: Agent server URL
        session_key: API key for the agent server

    Returns:
        BashCommandResult with found=True if command result was retrieved
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


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying an automation run's status."""

    verified: bool
    success: bool | None = None  # None if not verified
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


async def verify_run_on_agent_server(
    agent_url: str,
    session_key: str,
    run_id: str | None = None,
) -> VerificationResult:
    """Verify an automation run's status by querying an agent server directly.

    This function queries the agent server's bash command history to determine
    if the automation command has completed and what its exit status was.

    Use this for local mode where the agent server is persistent and we don't
    need to discover the sandbox first.

    Args:
        agent_url: Agent server URL
        session_key: API key for the agent server
        run_id: Optional run ID for logging

    Returns:
        VerificationResult with the verification outcome
    """
    agent_url = agent_url.rstrip("/")
    extra = log_extra(run_id=run_id)

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get last bash command result
        bash_result = await get_last_bash_command_result(client, agent_url, session_key)

        if not bash_result.found:
            logger.warning(
                "Could not find bash command result: %s",
                bash_result.error,
                extra=extra,
            )
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

        return VerificationResult(
            verified=True,
            success=success,
            exit_code=bash_result.exit_code,
            stdout=bash_result.stdout,
            stderr=bash_result.stderr,
        )
