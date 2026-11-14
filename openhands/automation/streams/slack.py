"""Slack Socket Mode as a stream source.

Slack holds the connection open and pushes envelopes down it, so there is no
inbound request to authenticate: the app token is the credential, and the four
rules below are what keep the stream honest. Because `emit()` is an in-process
call there is no forwarding hop that can fail after the envelope is acked,
which is the failure an out-of-process bridge cannot avoid.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from openhands.automation.config import SlackAppSettings, StreamSettings
from openhands.automation.event_schemas.custom import CustomWebhookEvent
from openhands.automation.ingest import AcceptedEvent
from openhands.automation.streams.base import (
    Emit,
    StreamConfigError,
    record_health,
)
from openhands.automation.utils.time import utcnow


logger = logging.getLogger("automation.streams.slack")

# Widen once the transport is proven; every other event type is acked and
# dropped.
SUPPORTED_EVENT_TYPES = frozenset({"app_mention"})


@dataclass
class SlackStreamProvider:
    """One Socket Mode connection to one Slack app."""

    org_id: uuid.UUID
    app_token: str
    bot_token: str
    team_id: str
    bot_user_id: str
    thread_context_max_messages: int = 50
    thread_context_max_chars: int = 12_000
    source: str = "slack"

    @property
    def name(self) -> str:
        return f"{self.source}:{self.team_id}"

    async def run(self, emit: Emit, shutdown: asyncio.Event) -> None:
        web_client = AsyncWebClient(token=self.bot_token)
        await self.assert_identity(web_client)

        # Constructed inside the try: it opens an aiohttp session eagerly, so a
        # failed connect must still be closed rather than left to the GC.
        client = SocketModeClient(app_token=self.app_token, web_client=web_client)
        try:
            client.socket_mode_request_listeners.append(self.build_listener(emit))
            await client.connect()
            # Connecting ends any failure streak. Without this the count is
            # lifetime failures rather than consecutive ones, and a source that
            # drops once a week would creep to the maximum backoff and stay
            # there -- `run()` only returns cleanly at shutdown.
            record_health(self.name, last_connected_at=utcnow(), consecutive_failures=0)
            logger.info("Slack Socket Mode connected for team=%s", self.team_id)
            await shutdown.wait()
        finally:
            await client.close()

    async def assert_identity(self, web_client: AsyncWebClient) -> None:
        """Refuse to start unless the token belongs to the configured app.

        A mis-pasted token would otherwise open a socket onto someone else's
        workspace and bridge its mentions into this organization.
        """
        response = await web_client.auth_test()
        team_id = response.get("team_id")
        # `auth.test` names the bot's own user id `user_id`. `bot_user_id` is a
        # real Slack field, but it belongs to `oauth.v2.access`; reading it here
        # yields None for every token, so the check below could never pass.
        bot_user_id = response.get("user_id")
        if team_id != self.team_id or bot_user_id != self.bot_user_id:
            raise StreamConfigError(
                f"Slack token identifies team={team_id} bot_user={bot_user_id}, "
                f"but the source is configured for team={self.team_id} "
                f"bot_user={self.bot_user_id}"
            )

    def build_listener(self, emit: Emit):
        """Build the Socket Mode request listener bound to this `emit`."""

        async def listener(
            client: AsyncBaseSocketModeClient, request: SocketModeRequest
        ) -> None:
            await self.handle(client, request, emit)

        return listener

    async def handle(
        self,
        client: AsyncBaseSocketModeClient,
        request: SocketModeRequest,
        emit: Emit,
    ) -> None:
        """Ack the envelope, then route it if it is an event we act on."""
        # Ack first, before any other work: Slack redelivers anything unacked,
        # and an ack it does not get within three seconds is a redelivery.
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=request.envelope_id)
        )
        if request.type != "events_api":
            return

        event = self.accepted_event(request.payload)
        if event is not None:
            event = await self.with_thread_context(
                event, getattr(client, "web_client", None)
            )
            await emit(event)

    async def with_thread_context(
        self, event: AcceptedEvent, web_client: AsyncWebClient | None
    ) -> AcceptedEvent:
        """Attach the thread a mention was posted in, leaving the envelope as is.

        Best effort: a bare mention, or a thread Slack will not return, keeps
        the mention-only event.
        """
        slack_event = event.payload.get("event") or {}
        thread_ts = slack_event.get("thread_ts")
        channel = slack_event.get("channel")
        trigger_ts = slack_event.get("ts")
        if not thread_ts or not channel:
            return event
        if web_client is None:
            logger.warning("No Slack Web API client; omitting thread context")
            return event
        try:
            raw_messages, complete = await _fetch_thread(
                web_client, channel, thread_ts, trigger_ts
            )
        except Exception as exc:  # noqa: BLE001 - context is best effort
            logger.warning(
                "Could not fetch Slack thread context for channel=%s ts=%s: %s",
                channel,
                thread_ts,
                exc,
            )
            return event
        messages = [
            _transcript_message(message)
            for message in raw_messages
            if not trigger_ts or _ts_key(message.get("ts")) <= _ts_key(trigger_ts)
        ]
        kept, omitted = bound_thread_messages(
            messages,
            max_messages=self.thread_context_max_messages,
            max_chars=self.thread_context_max_chars,
        )
        return replace(
            event,
            context={
                "slack_thread": {
                    "messages": kept,
                    "omitted": omitted,
                    "incomplete": not complete,
                }
            },
        )

    def accepted_event(self, envelope: dict[str, Any]) -> AcceptedEvent | None:
        """Interpret a Slack event envelope, or None if it is not for us.

        The payload handed on is the whole envelope, not the inner event: that
        is the shape Slack's own webhook delivers, so a trigger filter reads
        the same fields either way (`team_id == '...'` is a top-level one).
        """
        if envelope.get("team_id") != self.team_id:
            # The socket is per-app, so this should not happen; if it does,
            # the app is not the one we think it is.
            logger.warning(
                "Dropping Slack event for team=%s on the team=%s connection",
                envelope.get("team_id"),
                self.team_id,
            )
            return None

        event = envelope.get("event") or {}
        event_key = event.get("type")
        if event_key not in SUPPORTED_EVENT_TYPES:
            return None

        # Drop the bot's own messages, or an @-mention it writes triggers the
        # automation that wrote it.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return None

        return AcceptedEvent(
            source=self.source,
            event_key=event_key,
            payload=envelope,
            # Slack's own delivery id. `accept_event()` deduplicates on it, so
            # a redelivered envelope creates no second run.
            provider_event_id=envelope.get("event_id"),
            # What the *run* is handed, which is not the same thing as what
            # trigger filters read. The HTTP path parses a custom webhook into
            # a CustomWebhookEvent, and `accept_event()` persists that model
            # rather than the raw body -- so a script finds the envelope at
            # `payload`. Emitting the bare envelope here would put the same
            # data one level higher, and every existing Slack automation would
            # read nothing at all.
            parsed_event=CustomWebhookEvent(
                _event_key=event_key,
                payload=envelope,
                source_override=self.source,
            ),
        )


# `conversations.replies` returns at most this many messages per page.
THREAD_PAGE_SIZE = 200
# A mention deeper into a thread than this many pages gets incomplete context
# rather than an unbounded number of Web API calls.
THREAD_MAX_PAGES = 10


async def _fetch_thread(
    web_client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    trigger_ts: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read a thread oldest first, up to the trigger message.

    Returns the messages and whether they reach the trigger. A failure on the
    first page raises; a later one returns what was read as incomplete.
    """
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(THREAD_MAX_PAGES):
        try:
            response = await web_client.conversations_replies(
                channel=channel, ts=thread_ts, limit=THREAD_PAGE_SIZE, cursor=cursor
            )
        except Exception as exc:
            if not messages:
                raise
            logger.warning(
                "Slack thread channel=%s ts=%s stopped after %d messages: %s",
                channel,
                thread_ts,
                len(messages),
                exc,
            )
            return messages, False
        page = response.get("messages") or []
        messages.extend(page)
        if trigger_ts and any(
            _ts_key(message.get("ts")) >= _ts_key(trigger_ts) for message in page
        ):
            return messages, True
        if not response.get("has_more"):
            return messages, True
        next_cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            return messages, False
        cursor = next_cursor
    return messages, False


def _ts_key(ts: Any) -> Decimal:
    try:
        return Decimal(str(ts))
    except InvalidOperation:
        return Decimal(0)


def _transcript_message(message: dict[str, Any]) -> dict[str, Any]:
    files = [
        item.get("name") or item.get("title")
        for item in message.get("files") or []
        if item.get("name") or item.get("title")
    ]
    attachments = [
        item.get("title") or item.get("fallback")
        for item in message.get("attachments") or []
        if item.get("title") or item.get("fallback")
    ]
    return {
        "author": message.get("user") or message.get("bot_id") or "unknown",
        "timestamp": message.get("ts", ""),
        "text": message.get("text", ""),
        "files": files + attachments,
    }


def _encoded_len(value: Any) -> int:
    # The dispatcher hands the run its payload as one environment variable
    # serialised with a default `json.dumps`, so measure the same encoding:
    # escaped non-ASCII is what counts against the kernel's per-string limit.
    return len(json.dumps(value))


def _trim_message(message: dict[str, Any], budget: int) -> dict[str, Any] | None:
    """The message with its text cut to fit *budget*, or None if it cannot fit."""
    for files in (message["files"], []):
        candidate = {**message, "files": files, "text": "…"}
        if _encoded_len(candidate) > budget:
            continue
        text = message["text"]
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            candidate["text"] = text[:mid] + "…"
            if _encoded_len(candidate) <= budget:
                low = mid
            else:
                high = mid - 1
        candidate["text"] = text[:low] + "…"
        return candidate
    return None


def bound_thread_messages(
    messages: list[dict[str, Any]], *, max_messages: int, max_chars: int
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newest messages that fit the bounds; return them and the
    number of older messages dropped.

    The oldest kept message is cut short rather than dropped when only part of
    it fits, so one long message does not cost the context before the mention.
    """
    omitted = max(0, len(messages) - max_messages)
    kept = messages[omitted:]
    while kept and _encoded_len(kept) > max_chars:
        rest = kept[1:]
        # `[a, ...rest]` adds ", " to `rest`'s encoding; `[a]` adds "[]".
        budget = max_chars - (_encoded_len(rest) + 2 if rest else 2)
        trimmed = _trim_message(kept[0], budget)
        if trimmed is not None:
            kept = [trimmed, *rest]
            break
        kept = rest
        omitted += 1
    return kept, omitted


def build_slack_providers(settings: StreamSettings) -> list[SlackStreamProvider]:
    """Build one provider per configured Slack app."""
    return [_provider(app) for app in settings.slack_apps]


def _provider(app: SlackAppSettings) -> SlackStreamProvider:
    return SlackStreamProvider(
        org_id=app.org_id,
        app_token=app.app_token,
        bot_token=app.bot_token,
        team_id=app.team_id,
        bot_user_id=app.bot_user_id,
        thread_context_max_messages=app.thread_context_max_messages,
        thread_context_max_chars=app.thread_context_max_chars,
    )
