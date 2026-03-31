"""Utility for setting sandbox automation metadata via the service API.

This module provides functions to associate automation metadata with sandboxes
in OH Cloud, allowing conversations created within those sandboxes to inherit
the automation context (trigger type, automation ID, etc.).
"""

import logging
from typing import Any

import httpx


logger = logging.getLogger("automation.utils.sandbox_metadata")


async def set_sandbox_automation_metadata(
    api_url: str,
    service_key: str,
    sandbox_id: str,
    automation_id: str | None = None,
    automation_name: str | None = None,
    trigger_type: str | None = None,
    run_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> bool:
    """Set automation metadata on a sandbox via the service API.

    This allows OH Cloud to associate conversations created in the sandbox
    with the automation that triggered them. The metadata is stored in the
    sandbox_automation_metadata table and copied to conversations via webhooks.

    This is a best-effort operation - failures are logged but do not fail the dispatch.

    Args:
        api_url: The OpenHands API base URL
        service_key: The service API key for authentication (AUTOMATIONS_SERVICE_KEY)
        sandbox_id: The sandbox ID to set metadata on
        automation_id: The automation definition ID
        automation_name: Human-readable name of the automation
        trigger_type: The trigger configuration (JSON string or type name)
        run_id: The specific automation run ID
        extra_metadata: Additional metadata as key-value pairs

    Returns:
        True if metadata was set successfully, False otherwise
    """
    if not service_key:
        logger.debug("No service key configured, skipping sandbox metadata")
        return False

    url = f"{api_url.rstrip('/')}/api/service/sandboxes/{sandbox_id}/automation-metadata"
    headers = {
        "X-Service-API-Key": service_key,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "automation_id": automation_id,
        "automation_name": automation_name,
        "trigger_type": trigger_type,
        "run_id": run_id,
    }
    if extra_metadata:
        payload["extra_metadata"] = extra_metadata

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(url, headers=headers, json=payload)
            response.raise_for_status()
            logger.info(
                "Set sandbox automation metadata",
                extra={
                    "sandbox_id": sandbox_id,
                    "automation_id": automation_id,
                    "run_id": run_id,
                },
            )
            return True
    except httpx.HTTPStatusError as e:
        logger.warning(
            "Failed to set sandbox automation metadata: HTTP %s - %s",
            e.response.status_code,
            e.response.text,
            extra={"sandbox_id": sandbox_id, "automation_id": automation_id},
        )
    except Exception as e:
        logger.warning(
            "Failed to set sandbox automation metadata: %s",
            str(e),
            extra={"sandbox_id": sandbox_id, "automation_id": automation_id},
        )

    return False
