"""Sending another turn to a conversation the agent server already holds.

`run: true` appends the message and starts the loop only if it is not already
running. Every failure returns False so the caller starts a run instead -- a
reaped sandbox is ordinary, not an error.

A continue does not run the automation's script, so `compose_turn` is the only
thing the agent sees of the event. It renders the message a human actually
wrote; the verbatim payload is the last resort, not the default.
"""

import asyncio
import json
import logging
import time
from typing import Any, Final

import httpx

from openhands.automation.backends import get_backend
from openhands.automation.config import get_config
from openhands.automation.models import AutomationRun
from openhands.automation.utils.log_context import log_extra
from openhands.automation.utils.sandbox import get_sandbox_agent_url, resume_sandbox


logger = logging.getLogger("automation.conversation_turn")

# Short: the caller holds the subject's row lock for the length of this call.
TURN_TIMEOUT_SECONDS: Final[int] = 15

# An idle sandbox is paused, not deleted, so the conversation is still there.
# Waiting for it costs lock time; losing the thread's memory costs the user
# more. Past the budget we give up and the caller starts a run, as before.
RESUME_WAIT_SECONDS: Final[int] = 20
RESUME_POLL_SECONDS: Final[int] = 2


# Where a human-authored message sits in a webhook payload, most specific
# first. A custom webhook resolves no provider descriptor, so there is nothing
# to ask; probing keeps the common sources readable without teaching the
# service every payload shape, and `turn_text_expr` covers the rest.
# The subset of _BODY_PATHS that really is somebody commenting.
_COMMENT_BODY_PATHS = ("comment.body", "review.body")
_BODY_PATHS = (
    "comment.body",
    "review.body",
    "issue.body",
    "pull_request.body",
    "discussion.body",
    "event.text",
    "message.text",
    "text",
)
_AUTHOR_PATHS = (
    "comment.user.login",
    "review.user.login",
    "sender.login",
    "issue.user.login",
    "pull_request.user.login",
    "event.user",
    "user",
)
_URL_PATHS = (
    "comment.html_url",
    "review.html_url",
    "issue.html_url",
    "pull_request.html_url",
    "discussion.html_url",
)
_NUMBER_PATHS = ("issue.number", "pull_request.number", "discussion.number")


def _dig(node: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _first_hit(
    payload: dict[str, Any], paths: tuple[str, ...]
) -> tuple[Any, str | None]:
    """First non-empty hit and the path that produced it.

    Typed sources (GitHub) reach here wrapped as {"event_key", "payload"};
    Slack arrives as the bare envelope. Probing both spares the caller the
    difference.

    Paths drive the outer loop: `paths` is ordered most-specific-first, and
    that ordering is the point. Rooting the outer loop instead would let a
    generic key at the top level beat the specific one nested under `payload`.
    """
    roots = [payload]
    inner = payload.get("payload")
    if isinstance(inner, dict):
        roots.append(inner)
    for path in paths:
        for root in roots:
            value = _dig(root, path)
            if isinstance(value, str) and value.strip():
                return value.strip(), path
            if isinstance(value, int) and not isinstance(value, bool):
                return value, path
    return None, None


def _first(payload: dict[str, Any], paths: tuple[str, ...]) -> Any:
    """`_first_hit` for callers that do not care which path matched."""
    return _first_hit(payload, paths)[0]


def _render_message(
    source: str,
    event_key: str,
    payload: dict[str, Any],
) -> str | None:
    """The event as the message a human wrote, or None if none is recognisable."""
    body, body_path = _first_hit(payload, _BODY_PATHS)
    if not isinstance(body, str):
        return None

    author = _first(payload, _AUTHOR_PATHS)
    repo = _first(payload, ("repository.full_name",))
    number = _first(payload, _NUMBER_PATHS)
    url = _first(payload, _URL_PATHS)

    who = f"@{author}" if author else "Someone"
    # Only a comment was commented. Anything else keeps its event key, which is
    # the one word that says what actually happened -- an opened pull request
    # announced as a comment misleads the agent about what it is answering.
    what = "commented on" if body_path in _COMMENT_BODY_PATHS else f"(`{event_key}`) on"
    if repo and number:
        header = f"{who} {what} {repo}#{number}"
    elif repo:
        header = f"{who} {what} {repo}"
    else:
        header = f"{who} sent a new `{source}` message (`{event_key}`)"

    parts = [f"{header}:", "", body]
    if url:
        parts += ["", str(url)]
    return "\n".join(parts)


def compose_turn(
    source: str,
    event_key: str,
    event_payload: dict[str, Any] | None,
    *,
    override: str | None = None,
) -> str:
    """Render an event as the text of a follow-up turn.

    The trigger's own `turn_text_expr` wins; then the message the payload
    obviously carries; and only a shape nothing recognises falls back to the
    verbatim JSON. Dumping the payload unconditionally buried the one line a
    human wrote under ~15 KB of webhook metadata, every turn.
    """
    if override:
        return override
    if event_payload:
        rendered = _render_message(source, event_key, event_payload)
        if rendered is not None:
            return rendered
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
    api_url = get_config().service.openhands_api_base_url
    resolved = await get_sandbox_agent_url(client, api_url, api_key, run.sandbox_id)
    if resolved is not None:
        return resolved
    # Not RUNNING. It may be paused rather than gone, in which case the
    # conversation survives and only the sandbox has to come back.
    return await _resume_and_wait(client, api_url, api_key, run.sandbox_id)


async def _resume_and_wait(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    sandbox_id: str,
) -> tuple[str, str] | None:
    """Resume a paused sandbox and wait for its agent server, within a budget."""
    if not await resume_sandbox(client, api_url, api_key, sandbox_id):
        return None

    deadline = time.monotonic() + RESUME_WAIT_SECONDS
    while True:
        resolved = await get_sandbox_agent_url(client, api_url, api_key, sandbox_id)
        if resolved is not None:
            logger.info("Resumed sandbox %s to continue its conversation", sandbox_id)
            return resolved
        if time.monotonic() >= deadline:
            logger.info(
                "Sandbox %s did not come back within %.0fs; falling back to a run",
                sandbox_id,
                RESUME_WAIT_SECONDS,
            )
            return None
        await asyncio.sleep(RESUME_POLL_SECONDS)


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
