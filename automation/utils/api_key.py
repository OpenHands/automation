"""API key utilities for automation runs."""

import logging
from typing import TYPE_CHECKING

import httpx

from automation.config import get_settings


if TYPE_CHECKING:
    from automation.models import AutomationRun

logger = logging.getLogger(__name__)


class APIKeyError(Exception):
    """Exception raised when API key retrieval fails."""

    pass


async def get_api_key_for_automation_run(run: "AutomationRun") -> str:
    """Get an API key for executing an automation run.

    Creates a temporary API key for the user/org associated with the
    automation run by calling the OpenHands SaaS service API.

    Args:
        run: The automation run to get an API key for. Must have its
            `automation` relationship loaded with user_id and org_id.

    Returns:
        The API key string for authenticating with OpenHands.

    Raises:
        APIKeyError: If the API key cannot be retrieved.
        ValueError: If the run's automation relationship is not loaded.
    """
    if run.automation is None:
        raise ValueError(
            "AutomationRun.automation relationship must be loaded "
            "to retrieve user_id and org_id"
        )

    settings = get_settings()
    user_id = run.automation.user_id
    org_id = run.automation.org_id

    url = (
        f"{settings.openhands_api_base_url}/api/service/users/{user_id}"
        f"/orgs/{org_id}/api-keys"
    )

    headers = {
        "X-Service-API-Key": settings.service_key,
        "Content-Type": "application/json",
    }

    payload = {"name": "automation"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()

            data = response.json()
            api_key = data.get("key")

            if not api_key:
                raise APIKeyError(f"API key not found in response: {list(data.keys())}")

            logger.info(
                "Created API key for automation run %s (user=%s, org=%s)",
                run.id,
                user_id,
                org_id,
            )
            return api_key

    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to create API key for run %s: HTTP %s - %s",
            run.id,
            e.response.status_code,
            e.response.text,
            exc_info=True,
        )
        raise APIKeyError(f"HTTP {e.response.status_code}: {e.response.text}") from e

    except httpx.RequestError as e:
        logger.error(
            "Failed to create API key for run %s: %s",
            run.id,
            str(e),
            exc_info=True,
        )
        raise APIKeyError(f"Request failed: {str(e)}") from e
