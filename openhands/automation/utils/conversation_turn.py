"""Sending another turn to a conversation the agent server already holds.

The agent server owns conversations; this is the whole of what the automation
service does to one it did not create in-process. `POST .../events` with
`run: true` appends the message and starts the agent loop only when it is not
already running, which is exactly the "new turn on an idle conversation"
semantics `continue_conversation` depends on.

Every failure here is answered the same way -- return False, and let the caller
start a run instead. A sandbox is mortal: the conversation an event wants to
continue may have been reaped hours ago, and that is ordinary, not an error.
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

# Deliberately short. The caller holds a row lock on the subject's mapping for
# the length of this call, which serialises other events for the same Slack
# thread; a long stall there is worse than falling back to a run.
TURN_TIMEOUT_SECONDS = 15.0


def compose_turn(
    source: str,
    event_key: str,
    event_payload: dict[str, Any] | None,
) -> str:
    """Render an event as the text of a follow-up turn.

    The script that opened the conversation was handed this same payload as its
    event, so handing the agent the payload verbatim keeps the second turn in
    the shape it already understands. The service cannot do better: the
    automation's own prompt-building code runs inside the sandbox, and on a
    continue it does not run at all.
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
        # Side-effect free in local mode: the server is already running. The
        # cloud backend's version *creates a sandbox*, which is why this branch
        # is not shared.
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

    Returns True only when the agent server accepted the turn. False means the
    conversation could not be reached -- gone, unreachable, or on a sandbox
    that no longer exists -- and the caller should create a run.
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
                    # Start the agent loop unless it is already running. Without
                    # this the message lands in history and nothing answers it.
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
