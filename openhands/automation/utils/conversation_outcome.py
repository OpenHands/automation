"""Helpers for reading raw FinishTool responses from conversations."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from openhands.automation.backends import get_backend
from openhands.automation.config import get_config
from openhands.automation.models import AutomationRun
from openhands.automation.utils.sandbox import get_sandbox_agent_url


logger = logging.getLogger(__name__)

ACTION_EVENT_KIND = "openhands.sdk.event.llm_convertible.action.ActionEvent"
FINISH_TOOL_NAME = "finish"


def finish_tool_response_from_event(event: dict[str, Any]) -> Any | None:
    """Return the raw JSON-decoded arguments from a FinishTool action event."""
    if event.get("tool_name") != FINISH_TOOL_NAME:
        return None

    tool_call = event.get("tool_call")
    if not isinstance(tool_call, dict):
        return None

    raw_arguments = tool_call.get("arguments")
    if not isinstance(raw_arguments, str):
        return None

    try:
        return json.loads(raw_arguments)
    except json.JSONDecodeError:
        logger.warning("Could not decode finish tool arguments as JSON")
        return raw_arguments


def latest_finish_tool_response_from_events(
    events: list[dict[str, Any]],
) -> Any | None:
    """Return the latest finish action response from newest-first events."""
    for event in events:
        if event.get("tool_name") == FINISH_TOOL_NAME:
            return finish_tool_response_from_event(event)
    return None


async def fetch_latest_finish_tool_response(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    conversation_id: str,
) -> Any | None:
    """Fetch recent conversation actions and return the latest finish response."""
    response = await client.get(
        f"{agent_url.rstrip('/')}/api/conversations/{conversation_id}/events/search",
        params={
            "kind": ACTION_EVENT_KIND,
            "sort_order": "TIMESTAMP_DESC",
            "limit": 100,
        },
        headers={"X-Session-API-Key": session_key},
        timeout=30.0,
    )
    response.raise_for_status()
    page = response.json()
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        return None
    return latest_finish_tool_response_from_events(items)


async def fetch_latest_finish_tool_response_for_run(
    run: AutomationRun,
    conversation_id: str,
) -> Any | None:
    """Best-effort lookup of the latest raw FinishTool response for a run."""
    try:
        backend = get_backend(run)
        async with httpx.AsyncClient(timeout=60.0) as client:
            if backend.is_local_mode:
                ctx = await backend.get_execution_context(client)
                return await fetch_latest_finish_tool_response(
                    client,
                    ctx.agent_url,
                    ctx.session_key,
                    conversation_id,
                )

            if not run.sandbox_id:
                return None

            api_key = await backend.get_api_key()
            result = await get_sandbox_agent_url(
                client,
                get_config().service.openhands_api_base_url,
                api_key,
                run.sandbox_id,
            )
            if result is None:
                return None
            agent_url, session_key = result
            return await fetch_latest_finish_tool_response(
                client,
                agent_url,
                session_key,
                conversation_id,
            )
    except Exception as exc:
        logger.warning(
            "Could not fetch finish tool response for run %s conversation %s: %s",
            run.id,
            conversation_id,
            exc,
        )
        return None
