"""Helpers for reading structured task outcomes from conversations."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openhands.automation.backends import get_backend
from openhands.automation.config import get_config
from openhands.automation.models import AutomationRun
from openhands.automation.utils.sandbox import get_sandbox_agent_url


logger = logging.getLogger(__name__)

ACTION_EVENT_KIND = "openhands.sdk.event.llm_convertible.action.ActionEvent"
FINISH_TOOL_NAME = "finish"

TaskOutcomeStatus = Literal[
    "success",
    "partial_success",
    "blocked",
    "failed",
    "unknown",
]


class TaskOutcomeBlocker(BaseModel):
    type: str
    message: str
    recoverable: bool | None = None


class TaskOutcome(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: TaskOutcomeStatus
    summary: str = Field(alias="outcome_summary")
    blockers: list[TaskOutcomeBlocker] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    needs_user_action: bool = False
    reported_at: datetime | None = None
    terminal_reason: str | None = None


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_task_outcome_from_finish_event(
    event: dict[str, Any], *, reported_at: datetime | None = None
) -> TaskOutcome | None:
    """Parse a TaskOutcome from a serialized SDK FinishTool action event.

    The SDK does not persist ``action.structured_output``; per the structured
    output docs, the durable source is the original tool-call arguments. Preset
    schemas expose the semantic summary as ``outcome_summary`` because
    ``summary`` is reserved by the SDK for action metadata.
    """
    if event.get("tool_name") != FINISH_TOOL_NAME:
        return None

    tool_call = event.get("tool_call")
    if not isinstance(tool_call, dict):
        return None

    raw_arguments = tool_call.get("arguments")
    if not isinstance(raw_arguments, str):
        return None

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        logger.warning("Could not decode finish tool arguments as JSON")
        return None

    if not isinstance(arguments, dict):
        return None

    if "status" not in arguments or "outcome_summary" not in arguments:
        return None

    try:
        outcome = TaskOutcome.model_validate(arguments)
    except ValidationError as exc:
        logger.warning("Could not validate finish task outcome: %s", exc)
        return None

    recorded_at = reported_at or _event_timestamp(event)
    if recorded_at is not None:
        outcome = outcome.model_copy(update={"reported_at": recorded_at})
    return outcome


def latest_task_outcome_from_events(
    events: list[dict[str, Any]], *, reported_at: datetime | None = None
) -> TaskOutcome | None:
    """Parse the latest finish action from newest-first events, when present."""
    for event in events:
        if event.get("tool_name") == FINISH_TOOL_NAME:
            return parse_task_outcome_from_finish_event(event, reported_at=reported_at)
    return None


async def fetch_latest_task_outcome(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    conversation_id: str,
    *,
    reported_at: datetime | None = None,
) -> TaskOutcome | None:
    """Fetch recent conversation actions and parse the latest structured outcome."""
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
    return latest_task_outcome_from_events(items, reported_at=reported_at)


async def fetch_latest_task_outcome_for_run(
    run: AutomationRun,
    conversation_id: str,
    *,
    reported_at: datetime | None = None,
) -> TaskOutcome | None:
    """Best-effort lookup of the latest task outcome for a completed run."""
    try:
        backend = get_backend(run)
        async with httpx.AsyncClient(timeout=60.0) as client:
            if backend.is_local_mode:
                ctx = await backend.get_execution_context(client)
                return await fetch_latest_task_outcome(
                    client,
                    ctx.agent_url,
                    ctx.session_key,
                    conversation_id,
                    reported_at=reported_at,
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
            return await fetch_latest_task_outcome(
                client,
                agent_url,
                session_key,
                conversation_id,
                reported_at=reported_at,
            )
    except Exception as exc:
        logger.warning(
            "Could not fetch task outcome for run %s conversation %s: %s",
            run.id,
            conversation_id,
            exc,
        )
        return None
