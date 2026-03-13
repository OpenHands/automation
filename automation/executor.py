"""Executor: creates sandboxes and runs SDK code via the OpenHands V1 API.

Flow for each automation run:
1. Create a sandbox via POST /api/v1/sandboxes
2. Upload the SDK code tarball to the sandbox workspace
3. Execute the SDK script inside the sandbox
4. Track the conversation/sandbox ID in the run record
"""

import logging

import httpx

from automation.config import get_settings


logger = logging.getLogger(__name__)


class ExecutionResult:
    def __init__(
        self,
        *,
        success: bool,
        conversation_id: str | None = None,
        error: str | None = None,
    ):
        self.success = success
        self.conversation_id = conversation_id
        self.error = error


async def execute_automation(
    api_key: str,
    sdk_code_tarball_path: str,
) -> ExecutionResult:
    """Execute an automation by calling the OpenHands V1 API.

    This creates a conversation via POST /api/v1/app-conversations with the
    SDK code tarball path. The V1 API handles sandbox provisioning, code
    upload, and execution.

    For MVP, we use the app-conversations endpoint with a prompt that instructs
    the agent to download and execute the tarball. In the future, this will be
    replaced with direct sandbox API calls + SDK script execution.
    """
    settings = get_settings()
    base_url = settings.openhands_api_base_url
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # MVP: Create a conversation that executes the SDK code.
    # The initial message instructs the agent to fetch and run the tarball.
    # In production, this would use POST /api/v1/sandboxes + workspace upload.
    payload = {
        "initial_user_message": (
            f"Execute the SDK automation script from: {sdk_code_tarball_path}\n\n"
            "Steps:\n"
            "1. Download the tarball from the given path\n"
            "2. Extract it to /workspace/automation\n"
            "3. Run the main script with: python /workspace/automation/main.py\n"
            "4. Report the results"
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/api/v1/app-conversations",
                headers=headers,
                json=payload,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                conversation_id = data.get("id") or data.get("conversation_id")
                logger.info("Automation conversation started: %s", conversation_id)
                return ExecutionResult(
                    success=True, conversation_id=str(conversation_id)
                )

            error_msg = f"V1 API returned {resp.status_code}: {resp.text[:500]}"
            logger.error("Failed to start automation: %s", error_msg)
            return ExecutionResult(success=False, error=error_msg)

    except httpx.RequestError as e:
        error_msg = f"HTTP error calling V1 API: {e}"
        logger.error(error_msg)
        return ExecutionResult(success=False, error=error_msg)
