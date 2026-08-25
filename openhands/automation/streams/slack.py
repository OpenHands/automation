"""Slack Socket Mode as a stream source.

Ports the semantics of the OSS VM's `slack-socket-bot` bridge, which has run
this way in production since 2026-08-13. What changes is only where the code
lives: the bridge acked Slack and then POSTed the envelope to this service's
own webhook endpoint on a daemon thread with no retry, so a forward that timed
out dropped an event Slack would never redeliver. `emit()` is an in-process
call, so that failure mode ceases to exist.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from openhands.automation.config import SlackAppSettings, StreamSettings
from openhands.automation.ingest import AcceptedEvent
from openhands.automation.streams.base import (
    Emit,
    StreamConfigError,
    health_for,
)
from openhands.automation.utils.time import utcnow


logger = logging.getLogger("automation.streams.slack")

SOURCE = "slack"

# Widen once the transport is proven; every other event type is acked and
# dropped, exactly as the bridge does today.
SUPPORTED_EVENT_TYPES = frozenset({"app_mention"})


@dataclass
class SlackStreamProvider:
    """One Socket Mode connection to one Slack app."""

    org_id: uuid.UUID
    app_token: str
    bot_token: str
    team_id: str
    bot_user_id: str
    source: str = SOURCE

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
            health_for(self.name).last_connected_at = utcnow()
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
        bot_user_id = response.get("bot_user_id")
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
            await emit(event)

    def accepted_event(self, envelope: dict[str, Any]) -> AcceptedEvent | None:
        """Interpret a Slack event envelope, or None if it is not for us.

        The payload handed on is the whole envelope, not the inner event: that
        is what the bridge POSTs today, and what existing triggers filter on
        (`team_id == '...'` reads a top-level field).
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
        )


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
    )
