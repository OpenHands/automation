"""Tests for outbound WebSocket source CRUD endpoints and schema validation."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from openhands.automation.models import OutboundWebSocketSource, WebSocketStatus
from openhands.automation.schemas import (
    GenericWebSocketSource,
    SlackWebSocketSource,
    WebSocketSourceUpdate,
)


TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

BASE_URL = "/api/automation/v1/websocket-sources"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestGenericWebSocketSource:
    def test_valid_minimal(self):
        s = GenericWebSocketSource(
            name="My WS",
            source="my-ws",
            url="wss://example.com/events",
        )
        assert s.kind == "GenericWebSocketSource"
        assert s.event_key_expr == "type"
        assert s.payload_expr is None
        assert s.headers is None

    def test_url_must_be_wss(self):
        with pytest.raises(ValidationError, match="wss://"):
            GenericWebSocketSource(
                name="Bad URL", source="bad", url="http://example.com"
            )

    def test_ws_url_also_accepted(self):
        s = GenericWebSocketSource(name="WS", source="ws", url="ws://localhost:9000")
        assert s.url == "ws://localhost:9000"

    def test_reserved_source_rejected(self):
        with pytest.raises(ValidationError, match="reserved"):
            GenericWebSocketSource(
                name="GitHub WS", source="github", url="wss://example.com"
            )

    def test_invalid_jmespath_event_key_expr(self):
        with pytest.raises(ValidationError, match="JMESPath"):
            GenericWebSocketSource(
                name="Bad",
                source="bad",
                url="wss://example.com",
                event_key_expr="invalid((((",
            )

    def test_invalid_jmespath_filter_expr(self):
        with pytest.raises(ValidationError, match="JMESPath"):
            GenericWebSocketSource(
                name="Bad",
                source="bad",
                url="wss://example.com",
                filter_expr="invalid((((",
            )

    def test_source_normalised_to_lowercase(self):
        s = GenericWebSocketSource(
            name="WS", source="MySource", url="wss://example.com"
        )
        assert s.source == "mysource"

    def test_with_headers_and_payload_expr(self):
        s = GenericWebSocketSource(
            name="WS",
            source="my-ws",
            url="wss://example.com",
            headers={"Authorization": "Bearer token"},
            payload_expr="event",
        )
        assert s.headers == {"Authorization": "Bearer token"}
        assert s.payload_expr == "event"


class TestSlackWebSocketSource:
    def test_valid(self):
        s = SlackWebSocketSource(
            name="Slack",
            source="slack",
            app_token="xapp-1-abc123",
        )
        assert s.kind == "SlackWebSocketSource"
        assert s.event_key_expr == "payload.event.type"
        assert s.payload_expr == "payload.event"

    def test_app_token_must_start_with_xapp(self):
        with pytest.raises(ValidationError, match="xapp-"):
            SlackWebSocketSource(
                name="Slack", source="slack", app_token="xoxb-wrong-token"
            )

    def test_custom_event_key_expr(self):
        s = SlackWebSocketSource(
            name="Slack",
            source="slack",
            app_token="xapp-1-abc123",
            event_key_expr="payload.type",
        )
        assert s.event_key_expr == "payload.type"


class TestWebSocketSourceUpdate:
    def test_partial_update_allowed(self):
        u = WebSocketSourceUpdate(name="New Name")
        assert u.name == "New Name"
        assert u.enabled is None

    def test_invalid_jmespath_rejected(self):
        with pytest.raises(ValidationError, match="JMESPath"):
            WebSocketSourceUpdate(filter_expr="bad(((")


# ---------------------------------------------------------------------------
# Router (CRUD) — integration tests using async_client
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_socket_manager():
    """Ensure app.state has no socket_manager so the router skips notification."""
    from openhands.automation.app import app

    had_it = hasattr(app.state, "socket_manager")
    original = app.state.socket_manager if had_it else None
    if had_it:
        del app.state.socket_manager
    yield
    if had_it:
        app.state.socket_manager = original


class TestCreateWebSocketSource:
    async def test_create_generic_success(self, async_client, _no_socket_manager):
        resp = await async_client.post(
            BASE_URL,
            json={
                "kind": "GenericWebSocketSource",
                "name": "My Generic WS",
                "source": "my-ws",
                "url": "wss://example.com/events",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["kind"] == "GenericWebSocketSource"
        assert data["source"] == "my-ws"
        assert data["url"] == "wss://example.com/events"
        assert data["status"] == "DISCONNECTED"
        # Credentials not returned
        assert "headers" not in data
        assert "app_token" not in data

    async def test_create_slack_success(self, async_client, _no_socket_manager):
        resp = await async_client.post(
            BASE_URL,
            json={
                "kind": "SlackWebSocketSource",
                "name": "Slack Events",
                "source": "slack-prod",
                "app_token": "xapp-1-AAAAAAAAA-1111111111-aaaaaaaaaaaaaaaa",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["kind"] == "SlackWebSocketSource"
        assert data["event_key_expr"] == "payload.event.type"
        assert data["payload_expr"] == "payload.event"
        # app_token is never returned
        assert "app_token" not in data

    async def test_create_duplicate_source_returns_409(
        self, async_client, _no_socket_manager
    ):
        payload = {
            "kind": "GenericWebSocketSource",
            "name": "WS Source",
            "source": "dup-source",
            "url": "wss://example.com",
        }
        resp1 = await async_client.post(BASE_URL, json=payload)
        assert resp1.status_code == 201

        resp2 = await async_client.post(BASE_URL, json=payload)
        assert resp2.status_code == 409
        assert "already exists" in resp2.json()["detail"]

    async def test_create_notifies_socket_manager(self, async_client, async_session):
        """When a socket manager is present, it should be notified on create."""
        from openhands.automation.app import app

        mock_sm = AsyncMock()
        app.state.socket_manager = mock_sm

        try:
            resp = await async_client.post(
                BASE_URL,
                json={
                    "kind": "GenericWebSocketSource",
                    "name": "Notify WS",
                    "source": "notify-ws",
                    "url": "wss://example.com",
                },
            )
            assert resp.status_code == 201
            mock_sm.on_source_changed.assert_awaited_once()
        finally:
            del app.state.socket_manager

    async def test_create_disabled_source_does_not_notify(
        self, async_client, async_session
    ):
        from openhands.automation.app import app

        mock_sm = AsyncMock()
        app.state.socket_manager = mock_sm

        try:
            resp = await async_client.post(
                BASE_URL,
                json={
                    "kind": "GenericWebSocketSource",
                    "name": "Disabled WS",
                    "source": "disabled-ws",
                    "url": "wss://example.com",
                    "enabled": False,
                },
            )
            assert resp.status_code == 201
            mock_sm.on_source_changed.assert_not_awaited()
        finally:
            del app.state.socket_manager

    async def test_missing_kind_returns_422(self, async_client, _no_socket_manager):
        resp = await async_client.post(
            BASE_URL,
            json={"name": "WS", "source": "ws", "url": "wss://example.com"},
        )
        assert resp.status_code == 422

    async def test_slack_bad_token_returns_422(self, async_client, _no_socket_manager):
        resp = await async_client.post(
            BASE_URL,
            json={
                "kind": "SlackWebSocketSource",
                "name": "Slack",
                "source": "bad-slack",
                "app_token": "xoxb-not-an-app-token",
            },
        )
        assert resp.status_code == 422


class TestListWebSocketSources:
    async def test_list_empty(self, async_client):
        resp = await async_client.get(BASE_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["sources"] == []

    async def test_list_returns_own_org_sources(
        self, async_client, async_session, _no_socket_manager
    ):
        # Create two sources for the test org
        for i in range(2):
            await async_client.post(
                BASE_URL,
                json={
                    "kind": "GenericWebSocketSource",
                    "name": f"WS {i}",
                    "source": f"ws-{i}",
                    "url": "wss://example.com",
                },
            )

        # Create one for another org directly in DB (should not appear)
        other = OutboundWebSocketSource(
            org_id=OTHER_ORG_ID,
            name="Other WS",
            source="other-ws",
            kind="GenericWebSocketSource",
            url="wss://other.com",
            event_key_expr="type",
            status=WebSocketStatus.DISCONNECTED,
        )
        async_session.add(other)
        await async_session.commit()

        resp = await async_client.get(BASE_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert all(s["source"] in ("ws-0", "ws-1") for s in data["sources"])


class TestGetWebSocketSource:
    async def test_get_existing(self, async_client, async_session, _no_socket_manager):
        create = await async_client.post(
            BASE_URL,
            json={
                "kind": "GenericWebSocketSource",
                "name": "Get Me",
                "source": "get-me",
                "url": "wss://example.com",
            },
        )
        source_id = create.json()["id"]

        resp = await async_client.get(f"{BASE_URL}/{source_id}")
        assert resp.status_code == 200
        assert resp.json()["source"] == "get-me"

    async def test_get_other_org_returns_404(self, async_client, async_session):
        other = OutboundWebSocketSource(
            org_id=OTHER_ORG_ID,
            name="Other",
            source="other",
            kind="GenericWebSocketSource",
            url="wss://other.com",
            event_key_expr="type",
            status=WebSocketStatus.DISCONNECTED,
        )
        async_session.add(other)
        await async_session.commit()

        resp = await async_client.get(f"{BASE_URL}/{other.id}")
        assert resp.status_code == 404

    async def test_get_nonexistent_returns_404(self, async_client):
        resp = await async_client.get(f"{BASE_URL}/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateWebSocketSource:
    async def test_update_name_and_enabled(self, async_client, _no_socket_manager):
        create = await async_client.post(
            BASE_URL,
            json={
                "kind": "GenericWebSocketSource",
                "name": "Old Name",
                "source": "upd-ws",
                "url": "wss://example.com",
            },
        )
        source_id = create.json()["id"]

        resp = await async_client.patch(
            f"{BASE_URL}/{source_id}",
            json={"name": "New Name", "enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Name"
        assert data["enabled"] is False

    async def test_update_triggers_reconnect_on_enabled_change(
        self, async_client, _no_socket_manager
    ):
        from openhands.automation.app import app

        mock_sm = AsyncMock()
        app.state.socket_manager = mock_sm

        try:
            create = await async_client.post(
                BASE_URL,
                json={
                    "kind": "GenericWebSocketSource",
                    "name": "Reconnect Test",
                    "source": "recon-ws",
                    "url": "wss://example.com",
                },
            )
            source_id = create.json()["id"]
            mock_sm.on_source_changed.reset_mock()

            resp = await async_client.patch(
                f"{BASE_URL}/{source_id}", json={"enabled": False}
            )
            assert resp.status_code == 200
            mock_sm.on_source_changed.assert_awaited_once()
        finally:
            del app.state.socket_manager


class TestDeleteWebSocketSource:
    async def test_delete_success(self, async_client, _no_socket_manager):
        create = await async_client.post(
            BASE_URL,
            json={
                "kind": "GenericWebSocketSource",
                "name": "Delete Me",
                "source": "del-ws",
                "url": "wss://example.com",
            },
        )
        source_id = create.json()["id"]

        resp = await async_client.delete(f"{BASE_URL}/{source_id}")
        assert resp.status_code == 204

        get_resp = await async_client.get(f"{BASE_URL}/{source_id}")
        assert get_resp.status_code == 404

    async def test_delete_notifies_socket_manager(
        self, async_client, _no_socket_manager
    ):
        from openhands.automation.app import app

        mock_sm = AsyncMock()
        app.state.socket_manager = mock_sm

        try:
            create = await async_client.post(
                BASE_URL,
                json={
                    "kind": "GenericWebSocketSource",
                    "name": "Del Notify",
                    "source": "del-notify",
                    "url": "wss://example.com",
                },
            )
            source_id = create.json()["id"]
            mock_sm.reset_mock()

            await async_client.delete(f"{BASE_URL}/{source_id}")
            mock_sm.on_source_deleted.assert_awaited_once_with(uuid.UUID(source_id))
        finally:
            del app.state.socket_manager


class TestReconnectWebSocketSource:
    async def test_reconnect_enabled_source(self, async_client, _no_socket_manager):
        from openhands.automation.app import app

        mock_sm = AsyncMock()
        app.state.socket_manager = mock_sm

        try:
            create = await async_client.post(
                BASE_URL,
                json={
                    "kind": "GenericWebSocketSource",
                    "name": "Reconnect WS",
                    "source": "recon-ws2",
                    "url": "wss://example.com",
                },
            )
            source_id = create.json()["id"]
            mock_sm.reset_mock()

            resp = await async_client.post(f"{BASE_URL}/{source_id}/reconnect")
            assert resp.status_code == 202
            mock_sm.on_source_changed.assert_awaited_once()
        finally:
            del app.state.socket_manager

    async def test_reconnect_disabled_source_returns_400(
        self, async_client, _no_socket_manager
    ):
        create = await async_client.post(
            BASE_URL,
            json={
                "kind": "GenericWebSocketSource",
                "name": "Disabled",
                "source": "disabled-ws2",
                "url": "wss://example.com",
                "enabled": False,
            },
        )
        source_id = create.json()["id"]

        resp = await async_client.post(f"{BASE_URL}/{source_id}/reconnect")
        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# SocketManager unit tests
# ---------------------------------------------------------------------------


class TestSocketManagerDispatch:
    """Unit tests for the dispatch pipeline without real WebSocket connections."""

    async def test_dispatch_pre_filter_drops_non_matching(self, async_session_factory):
        """Events that fail filter_expr should be silently dropped."""
        from openhands.automation.socket_manager import SocketManager

        sm = SocketManager(async_session_factory)

        source = OutboundWebSocketSource(
            id=uuid.uuid4(),
            org_id=TEST_ORG_ID,
            name="Test",
            source="test-ws",
            kind="GenericWebSocketSource",
            event_key_expr="type",
            filter_expr="type == 'allowed'",
            status=WebSocketStatus.CONNECTED,
        )

        # This message has type='blocked' so filter should drop it
        with patch(
            "openhands.automation.socket_manager.get_event_automations",
            new_callable=AsyncMock,
        ) as mock_get:
            await sm._dispatch(source, TEST_ORG_ID, {"type": "blocked"})
            mock_get.assert_not_called()

    async def test_dispatch_unwraps_payload_via_payload_expr(
        self, async_session_factory
    ):
        """payload_expr should unwrap the envelope before passing to automations."""
        from openhands.automation.socket_manager import SocketManager

        sm = SocketManager(async_session_factory)

        source = OutboundWebSocketSource(
            id=uuid.uuid4(),
            org_id=TEST_ORG_ID,
            name="Test",
            source="test-ws",
            kind="SlackWebSocketSource",
            event_key_expr="payload.event.type",
            payload_expr="payload.event",
            status=WebSocketStatus.CONNECTED,
        )

        raw_msg = {
            "envelope_id": "abc",
            "type": "events_api",
            "payload": {
                "event": {
                    "type": "message",
                    "text": "hello thems-fightin-words",
                    "channel": "C123",
                }
            },
        }

        async def fake_get_event_automations(org_id, source_name, session):
            return []

        with patch(
            "openhands.automation.socket_manager.get_event_automations",
            side_effect=fake_get_event_automations,
        ):
            await sm._dispatch(source, TEST_ORG_ID, raw_msg)
            # No automations match, but the key extraction + unwrap path ran.

    async def test_dispatch_drops_non_string_event_key(self, async_session_factory):
        """If event_key_expr returns a non-string, the event should be dropped."""
        from openhands.automation.socket_manager import SocketManager

        sm = SocketManager(async_session_factory)
        source = OutboundWebSocketSource(
            id=uuid.uuid4(),
            org_id=TEST_ORG_ID,
            name="Test",
            source="test-ws",
            kind="GenericWebSocketSource",
            event_key_expr="metadata",  # returns a dict, not a string
            status=WebSocketStatus.CONNECTED,
        )

        with patch(
            "openhands.automation.socket_manager.get_event_automations",
            new_callable=AsyncMock,
        ) as mock_get:
            await sm._dispatch(
                source,
                TEST_ORG_ID,
                {"type": "message", "metadata": {"key": "val"}},
            )
            mock_get.assert_not_called()
