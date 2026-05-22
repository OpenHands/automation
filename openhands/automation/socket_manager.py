"""Outbound WebSocket connection manager.

Maintains persistent client-initiated WebSocket connections for each enabled
OutboundWebSocketSource.  When a message arrives, it is dispatched through the
same trigger-matching pipeline used by inbound webhooks.

Connection lifecycle per source
--------------------------------
1. ``_run_source_loop``: outer reconnect loop with exponential back-off.
2. ``_connect_and_receive``: kind-specific connect + receive dispatcher.
   - ``_receive_generic``: static-URL sources.
   - ``_receive_slack``: Slack Socket Mode sources (dynamic URL via
     ``apps.connections.open``, per-message envelope ACKs).
3. ``_dispatch``: apply pre-filter → extract event key → unwrap payload →
   find matching automations → create PENDING runs.

ACK before dispatch
-------------------
For sources that require acknowledgements (currently Slack), the ACK is sent
immediately on receipt, *before* any automation-matching or DB writes.  This
ensures we never miss an ACK deadline due to slow dispatch logic.

Secret handling
---------------
Headers stored on generic sources may contain sensitive values.  They are
passed verbatim to the WebSocket upgrade request.  Future work: support
``${SECRET_NAME}`` placeholders resolved against the secrets store.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import jmespath
import websockets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.filter_eval import evaluate_filter
from openhands.automation.models import OutboundWebSocketSource, WebSocketStatus
from openhands.automation.trigger_matcher import matches_trigger
from openhands.automation.utils.webhook import (
    create_automation_run,
    get_event_automations,
)


logger = logging.getLogger("automation.socket_manager")

# Reconnect back-off: min 1 s, doubles each attempt, capped at 5 min
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 300.0
_BACKOFF_FACTOR = 2.0

# Maximum consecutive failures before a source is marked ERROR and paused
_MAX_FAILURES = 10


class SocketManager:
    """Manages all outbound WebSocket connections for the service.

    One ``asyncio.Task`` is maintained per enabled source.  Tasks are
    created on service startup and on API-driven changes (create/update/delete).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        # source_id → running connection task
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public interface (called from app lifespan and API router)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load all enabled sources from the DB and open connections."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(OutboundWebSocketSource).where(
                    OutboundWebSocketSource.enabled == True  # noqa: E712
                )
            )
            sources = result.scalars().all()

        logger.info("SocketManager starting %d source(s)", len(sources))
        for source in sources:
            self._start_task(source.id, source.org_id, source.kind)

    async def stop(self) -> None:
        """Cancel all connection tasks and wait for them to finish."""
        logger.info("SocketManager stopping %d task(s)", len(self._tasks))
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def on_source_changed(self, source_id: uuid.UUID) -> None:
        """Called when a source is created or updated.

        Cancels any existing task and starts a fresh one so that config
        changes (new token, new URL, toggled enabled flag) take effect
        immediately.
        """
        await self._cancel_task(source_id)

        async with self._session_factory() as session:
            source = await session.get(OutboundWebSocketSource, source_id)

        if source is None or not source.enabled:
            return

        self._start_task(source_id, source.org_id, source.kind)

    async def on_source_deleted(self, source_id: uuid.UUID) -> None:
        """Cancel the task for a source that is about to be deleted."""
        await self._cancel_task(source_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_task(self, source_id: uuid.UUID, org_id: uuid.UUID, kind: str) -> None:
        task = asyncio.create_task(
            self._run_source_loop(source_id, org_id, kind),
            name=f"ws-source-{source_id}",
        )
        self._tasks[source_id] = task

        def _on_done(t: asyncio.Task) -> None:
            self._tasks.pop(source_id, None)
            if not t.cancelled() and t.exception():
                logger.error(
                    "WebSocket source task raised unhandled exception source_id=%s",
                    source_id,
                    exc_info=t.exception(),
                )

        task.add_done_callback(_on_done)

    async def _cancel_task(self, source_id: uuid.UUID) -> None:
        task = self._tasks.pop(source_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _set_status(
        self,
        source_id: uuid.UUID,
        status: WebSocketStatus,
        detail: str | None = None,
        connected_at: datetime | None = None,
    ) -> None:
        # DB write failures must not terminate the WebSocket connection —
        # we log at ERROR (with traceback via logger.exception) and continue.
        # CancelledError is re-raised so task shutdown is never masked.
        try:
            async with self._session_factory() as session:
                source = await session.get(OutboundWebSocketSource, source_id)
                if source is None:
                    return
                source.status = status
                source.status_detail = detail
                if connected_at is not None:
                    source.connected_at = connected_at
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to update status source_id=%s status=%s — "
                "DB write error; WebSocket connection continues.",
                source_id,
                status.value,
            )

    async def _record_event(self, source_id: uuid.UUID) -> None:
        # Non-fatal: a missed last_event_at timestamp is acceptable.
        # CancelledError is re-raised so task shutdown is never masked.
        try:
            async with self._session_factory() as session:
                source = await session.get(OutboundWebSocketSource, source_id)
                if source:
                    source.last_event_at = datetime.now(UTC)
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to update last_event_at source_id=%s — non-fatal, continuing.",
                source_id,
            )

    # ------------------------------------------------------------------
    # Outer reconnect loop
    # ------------------------------------------------------------------

    async def _run_source_loop(
        self, source_id: uuid.UUID, org_id: uuid.UUID, kind: str
    ) -> None:
        """Outer loop: reconnect with exponential back-off on failure."""
        failures = 0
        delay = _BACKOFF_BASE

        while True:
            try:
                await self._set_status(source_id, WebSocketStatus.CONNECTING)
                await self._connect_and_receive(source_id, org_id, kind)
                # Clean exit (e.g. server-initiated disconnect) — reset failures
                failures = 0
                delay = _BACKOFF_BASE
            except asyncio.CancelledError:
                await self._set_status(source_id, WebSocketStatus.DISCONNECTED)
                raise
            except Exception as exc:
                failures += 1
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "WebSocket source disconnected source_id=%s failures=%d: %s",
                    source_id,
                    failures,
                    detail,
                )

                if failures >= _MAX_FAILURES:
                    logger.error(
                        "WebSocket source exceeded max failures, pausing source_id=%s",
                        source_id,
                    )
                    await self._set_status(
                        source_id,
                        WebSocketStatus.ERROR,
                        detail=f"Paused after {failures} consecutive failures. "
                        f"Last error: {detail}",
                    )
                    return

                await self._set_status(
                    source_id, WebSocketStatus.DISCONNECTED, detail=detail
                )

            logger.info(
                "WebSocket source will reconnect in %.0fs source_id=%s",
                delay,
                source_id,
            )
            await asyncio.sleep(delay)
            delay = min(delay * _BACKOFF_FACTOR, _BACKOFF_MAX)

    # ------------------------------------------------------------------
    # Kind-specific connect + receive
    # ------------------------------------------------------------------

    async def _connect_and_receive(
        self, source_id: uuid.UUID, org_id: uuid.UUID, kind: str
    ) -> None:
        async with self._session_factory() as session:
            source = await session.get(OutboundWebSocketSource, source_id)
            if source is None:
                return

        if kind == "slack":
            await self._receive_slack(source, org_id)
        else:
            await self._receive_generic(source, org_id)

    async def _receive_generic(
        self, source: OutboundWebSocketSource, org_id: uuid.UUID
    ) -> None:
        """Connect to a static wss:// URL and receive messages."""
        url = source.url
        if not url:
            raise ValueError(f"Generic source {source.id} has no url configured")

        extra_headers = list((source.headers or {}).items())

        logger.info(
            "Connecting to generic WebSocket url=%s source_id=%s", url, source.id
        )
        async with websockets.connect(url, additional_headers=extra_headers) as ws:
            await self._set_status(
                source.id,
                WebSocketStatus.CONNECTED,
                connected_at=datetime.now(UTC),
            )
            logger.info("Connected source_id=%s", source.id)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug(
                        "Non-JSON message from source_id=%s, skipping", source.id
                    )
                    continue

                await self._record_event(source.id)
                await self._dispatch(source, org_id, msg)

    async def _receive_slack(
        self, source: OutboundWebSocketSource, org_id: uuid.UUID
    ) -> None:
        """Connect via Slack Socket Mode.

        1. Calls ``apps.connections.open`` to obtain a fresh wss:// URL.
        2. Opens the WebSocket.
        3. ACKs every ``events_api`` envelope before dispatching.
        4. Handles ``disconnect`` events from Slack by raising to trigger
           a clean reconnect.
        """
        app_token = source.app_token
        if not app_token:
            raise ValueError(f"Slack source {source.id} has no app_token configured")

        # Fetch a fresh connection URL from Slack
        wss_url = await _slack_open_connection(app_token)
        logger.info("Obtained Slack Socket Mode URL for source_id=%s", source.id)

        async with websockets.connect(wss_url) as ws:
            # Slack sends a hello message immediately on connect
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            if hello.get("type") != "hello":
                raise RuntimeError(f"Expected Slack hello, got: {hello.get('type')!r}")

            await self._set_status(
                source.id,
                WebSocketStatus.CONNECTED,
                connected_at=datetime.now(UTC),
            )
            logger.info("Slack Socket Mode connected source_id=%s", source.id)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                # Slack may ask us to disconnect and reconnect cleanly
                if msg_type == "disconnect":
                    reason = msg.get("reason", "unknown")
                    logger.info(
                        "Slack requested disconnect reason=%s source_id=%s",
                        reason,
                        source.id,
                    )
                    raise _SlackReconnectRequested(reason)

                # ACK immediately before any dispatch work
                envelope_id = msg.get("envelope_id")
                if envelope_id:
                    await ws.send(json.dumps({"envelope_id": envelope_id}))

                if msg_type != "events_api":
                    continue

                await self._record_event(source.id)
                await self._dispatch(source, org_id, msg)

    # ------------------------------------------------------------------
    # Dispatch pipeline (shared by all kinds)
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        source: OutboundWebSocketSource,
        org_id: uuid.UUID,
        raw_msg: dict[str, Any],
    ) -> None:
        """Apply pre-filter, extract event key, unwrap, find automations, run.

        This mirrors the logic in event_router.py but operates on the raw
        WebSocket message rather than an HTTP request body.
        """
        # 1. Connection-level pre-filter (evaluated against raw message)
        if source.filter_expr:
            try:
                if not evaluate_filter(source.filter_expr, raw_msg):
                    return
            except Exception:
                logger.debug(
                    "Pre-filter raised for source_id=%s, dropping event",
                    source.id,
                    exc_info=True,
                )
                return

        # 2. Extract event key via event_key_expr
        try:
            event_key = jmespath.search(source.event_key_expr, raw_msg)
        except Exception:
            logger.debug(
                "event_key_expr failed for source_id=%s, dropping",
                source.id,
                exc_info=True,
            )
            return

        if not isinstance(event_key, str) or not event_key:
            logger.debug(
                "event_key_expr returned non-string for source_id=%s, dropping",
                source.id,
            )
            return

        # 3. Unwrap envelope to get the event payload for automation filtering
        if source.payload_expr:
            try:
                event_payload = jmespath.search(source.payload_expr, raw_msg)
            except Exception:
                logger.debug(
                    "payload_expr failed for source_id=%s, using raw", source.id
                )
                event_payload = raw_msg
        else:
            event_payload = raw_msg

        if not isinstance(event_payload, dict):
            logger.debug(
                "payload_expr returned non-dict for source_id=%s, skipping",
                source.id,
            )
            return

        # 4. Find matching automations and create runs
        async with self._session_factory() as session:
            automations = await get_event_automations(org_id, source.source, session)
            matched = [
                auto
                for auto, trigger in automations
                if matches_trigger(trigger, source.source, event_key, event_payload)
            ]

            if not matched:
                return

            logger.info(
                "WebSocket event matched %d automation(s) source=%s event_key=%s",
                len(matched),
                source.source,
                event_key,
            )
            for automation in matched:
                await create_automation_run(
                    automation, session, event_payload=event_payload
                )
            await session.commit()


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------


class _SlackReconnectRequested(Exception):
    """Raised when Slack sends a disconnect event, triggering a clean reconnect."""


async def _slack_open_connection(app_token: str) -> str:
    """Call Slack's apps.connections.open to get a fresh wss:// URL.

    Args:
        app_token: Slack App-Level Token (xapp-…).

    Returns:
        The wss:// URL to connect to.

    Raises:
        RuntimeError: if the Slack API call fails or returns an error.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://slack.com/api/apps.connections.open",
            headers={
                "Authorization": f"Bearer {app_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        raise RuntimeError(
            f"apps.connections.open failed: {data.get('error', 'unknown error')}"
        )

    url = data.get("url")
    if not url:
        raise RuntimeError("apps.connections.open returned no url")

    return url
