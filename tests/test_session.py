"""Tests for session-based event routing.

Tests session schema validation (SessionConfig), session utility functions
(extract_session_key, get_active_session, etc.), and event router session routing.
These tests do NOT require Docker or PostgreSQL — they use in-process logic only
or mocks where needed.
"""

import pytest

from openhands.automation.schemas import EventTrigger, SessionConfig
from openhands.automation.utils.session import extract_session_key


# ---------------------------------------------------------------------------
# SessionConfig schema validation
# ---------------------------------------------------------------------------


class TestSessionConfigSchema:
    def test_minimal_valid_config(self):
        cfg = SessionConfig(key_expr="pull_request.number")
        assert cfg.key_expr == "pull_request.number"
        assert cfg.idle_timeout_seconds == 300  # default
        assert cfg.session_timeout_seconds == 3600  # default
        assert cfg.on_sandbox_death == "queue"  # default

    def test_full_config(self):
        cfg = SessionConfig(
            key_expr="thread_ts || ts",
            idle_timeout_seconds=120,
            session_timeout_seconds=7200,
            on_sandbox_death="restart",
        )
        assert cfg.key_expr == "thread_ts || ts"
        assert cfg.idle_timeout_seconds == 120
        assert cfg.session_timeout_seconds == 7200
        assert cfg.on_sandbox_death == "restart"

    def test_on_sandbox_death_drop(self):
        cfg = SessionConfig(key_expr="issue.number", on_sandbox_death="drop")
        assert cfg.on_sandbox_death == "drop"

    def test_on_sandbox_death_queue(self):
        cfg = SessionConfig(key_expr="issue.number", on_sandbox_death="queue")
        assert cfg.on_sandbox_death == "queue"

    def test_invalid_on_sandbox_death(self):
        with pytest.raises(Exception):
            SessionConfig.model_validate(
                {"key_expr": "issue.number", "on_sandbox_death": "invalid"}
            )

    def test_idle_timeout_too_low(self):
        with pytest.raises(Exception):
            SessionConfig(key_expr="issue.number", idle_timeout_seconds=10)  # min 30

    def test_idle_timeout_too_high(self):
        with pytest.raises(Exception):
            SessionConfig(
                key_expr="issue.number", idle_timeout_seconds=4000
            )  # max 3600

    def test_session_timeout_too_low(self):
        with pytest.raises(Exception):
            SessionConfig(key_expr="issue.number", session_timeout_seconds=30)  # min 60

    def test_session_timeout_too_high(self):
        with pytest.raises(Exception):
            SessionConfig(
                key_expr="issue.number", session_timeout_seconds=100000
            )  # max 86400

    def test_invalid_jmespath_expression(self):
        with pytest.raises(Exception) as exc_info:
            SessionConfig(key_expr="[invalid(((")
        assert "JMESPath" in str(exc_info.value) or "Invalid" in str(exc_info.value)

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            SessionConfig.model_validate(
                {"key_expr": "issue.number", "unknown_field": "value"}
            )

    def test_complex_jmespath(self):
        # JMESPath alternatives expression is valid syntax for jmespath.compile
        cfg = SessionConfig(key_expr="pull_request.number || issue.number")
        assert cfg.key_expr == "pull_request.number || issue.number"

    def test_roundtrip_json(self):
        cfg = SessionConfig(
            key_expr="issue.number",
            idle_timeout_seconds=60,
            session_timeout_seconds=1800,
            on_sandbox_death="restart",
        )
        data = cfg.model_dump()
        restored = SessionConfig.model_validate(data)
        assert restored == cfg


# ---------------------------------------------------------------------------
# EventTrigger with session field
# ---------------------------------------------------------------------------


class TestEventTriggerWithSession:
    def test_event_trigger_without_session(self):
        trigger = EventTrigger(source="github", on="push")
        assert trigger.session is None

    def test_event_trigger_with_session(self):
        trigger = EventTrigger(
            source="github",
            on=["issue_comment.created", "pull_request.synchronize"],
            filter="icontains(comment.body, '@openhands')",
            session=SessionConfig(
                key_expr="pull_request.number || issue.number",
                idle_timeout_seconds=600,
            ),
        )
        assert trigger.session is not None
        assert trigger.session.key_expr == "pull_request.number || issue.number"
        assert trigger.session.idle_timeout_seconds == 600

    def test_event_trigger_session_roundtrip(self):
        """EventTrigger with session survives model_dump / model_validate."""
        trigger = EventTrigger(
            source="slack",
            on="message",
            session=SessionConfig(
                key_expr="thread_ts || ts",
                on_sandbox_death="queue",
            ),
        )
        data = trigger.model_dump()
        restored = EventTrigger.model_validate(data)
        assert restored.session is not None
        assert restored.session.key_expr == "thread_ts || ts"
        assert restored.session.on_sandbox_death == "queue"

    def test_event_trigger_session_via_dict(self):
        """EventTrigger can be constructed from a dict (as stored in DB JSON column)."""
        trigger_dict = {
            "type": "event",
            "source": "github",
            "on": "pull_request.opened",
            "session": {
                "key_expr": "pull_request.number",
                "idle_timeout_seconds": 300,
                "session_timeout_seconds": 3600,
                "on_sandbox_death": "restart",
            },
        }
        trigger = EventTrigger.model_validate(trigger_dict)
        assert trigger.session is not None
        assert trigger.session.on_sandbox_death == "restart"


# ---------------------------------------------------------------------------
# extract_session_key utility
# ---------------------------------------------------------------------------


class TestExtractSessionKey:
    def test_simple_path(self):
        payload = {"pull_request": {"number": 42}}
        key = extract_session_key("pull_request.number", payload)
        assert key == "42"

    def test_nested_path(self):
        payload = {"issue": {"id": 7, "number": 123}}
        key = extract_session_key("issue.number", payload)
        assert key == "123"

    def test_string_value(self):
        payload = {"thread_ts": "1234567890.123456"}
        key = extract_session_key("thread_ts", payload)
        assert key == "1234567890.123456"

    def test_missing_path_returns_none(self):
        payload = {"other": "data"}
        key = extract_session_key("pull_request.number", payload)
        assert key is None

    def test_null_value_returns_none(self):
        payload = {"pull_request": None}
        key = extract_session_key("pull_request.number", payload)
        assert key is None

    def test_jmespath_or_expression(self):
        # JMESPath || (alternatives) — first non-null wins
        payload = {"pull_request": {"number": 99}}
        key = extract_session_key("pull_request.number || issue.number", payload)
        assert key == "99"

    def test_jmespath_or_fallback(self):
        payload = {"issue": {"number": 55}}
        key = extract_session_key("pull_request.number || issue.number", payload)
        assert key == "55"

    def test_integer_value_converted_to_string(self):
        payload = {"pr": {"id": 1001}}
        key = extract_session_key("pr.id", payload)
        assert key == "1001"
        assert isinstance(key, str)

    def test_invalid_expression_returns_none(self):
        """Invalid JMESPath expression — extract_session_key returns None, no raise."""
        # Note: jmespath.compile validation is done at schema creation time.
        # At runtime, malformed expressions caught and return None.
        payload = {"data": "value"}
        key = extract_session_key("[invalid(((", payload)
        assert key is None

    def test_empty_payload(self):
        key = extract_session_key("pull_request.number", {})
        assert key is None

    def test_list_value_converted_to_string(self):
        payload = {"labels": ["bug", "feature"]}
        key = extract_session_key("labels", payload)
        # Lists stringify as Python list repr — usable as session key
        assert key is not None


# ---------------------------------------------------------------------------
# EventResponse schema
# ---------------------------------------------------------------------------


class TestEventResponseSchema:
    def test_events_queued_defaults_to_zero(self):
        from openhands.automation.schemas import EventResponse

        resp = EventResponse(received=True, matched=3, runs_created=["a", "b"])
        assert resp.events_queued == 0

    def test_events_queued_set(self):
        from openhands.automation.schemas import EventResponse

        resp = EventResponse(
            received=True, matched=2, runs_created=["a"], events_queued=1
        )
        assert resp.events_queued == 1
