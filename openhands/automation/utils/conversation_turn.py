"""Sending another turn to a conversation the agent server already holds.

`POST .../events` with `run: true` appends the message and starts the agent
loop only when it is not already running.

Every failure returns False so the caller starts a run instead: the sandbox
holding the conversation may have been reaped, which is ordinary.
"""

import json
import logging
from typing import Any

import httpx

from openhands.automation.backends import get_backend
from openhands.automation.config import get_config
from openhands.automation.models import AutomationRun
from openhands.automation.utils.log_context import log_extra
from openhands.automation.utils.sandbox import get_sandbox_agent_url


logger = logging.getLogger("automation.conversation_turn")

# Short: the caller holds the subject's row lock for the length of this call.
TURN_TIMEOUT_SECONDS = 15.0


def compose_turn(
    source: str,
    event_key: str,
    event_payload: dict[str, Any] | None,
) -> str:
    """Render an event as the text of a follow-up turn.

    The payload goes over verbatim: it is the shape the automation's script was
    handed for the first turn, and on a continue that script does not run.
    """
    body = json.dumps(event_payload or {}, indent=2, sort_keys=True, default=str)
    return (
        f"A new `{source}` event (`{event_key}`) arrived on the same subject as "
        f"this conversation.\n\n```json\n{body}\n```"
    )


async def _resolve_agent_server(
    run: AutomationRun,
    client: httpx.AsyncClient,
) -> tuple[str, str] | None:
    """Find the agent server holding this run's conversation."""
    backend = get_backend(run)
    if backend.is_local_mode:
        # Side-effect free here; the cloud backend's version creates a sandbox.
        ctx = await backend.get_execution_context(client)
        return ctx.agent_url, ctx.session_key

    if not run.sandbox_id:
        return None
    api_key = await backend.get_api_key()
    return await get_sandbox_agent_url(
        client,
        get_config().service.openhands_api_base_url,
        api_key,
        run.sandbox_id,
    )


async def send_conversation_turn(
    run: AutomationRun,
    conversation_id: str,
    text: str,
) -> bool:
    """Append a user message to an existing conversation and let it run.

    True only when the agent server accepted the turn.
    """
    extra = log_extra(run_id=str(run.id), sandbox_id=run.sandbox_id)
    try:
        async with httpx.AsyncClient(timeout=TURN_TIMEOUT_SECONDS) as client:
            resolved = await _resolve_agent_server(run, client)
            if resolved is None:
                logger.info(
                    "No agent server for conversation %s; falling back to a run",
                    conversation_id,
                    extra=extra,
                )
                return False

            agent_url, session_key = resolved
            response = await client.post(
                f"{agent_url.rstrip('/')}/api/conversations/{conversation_id}/events",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    # Without this the message lands in history unanswered.
                    "run": True,
                },
                headers={"X-Session-API-Key": session_key},
            )
            response.raise_for_status()
    except Exception as exc:
        logger.info(
            "Could not send a turn to conversation %s: %s",
            conversation_id,
            exc,
            extra=extra,
        )
        return False

    logger.info("Sent a turn to conversation %s", conversation_id, extra=extra)
    return True
