import json
from datetime import UTC, datetime

import httpx
import pytest

from openhands.automation.utils.conversation_outcome import (
    ACTION_EVENT_KIND,
    fetch_latest_task_outcome,
    latest_task_outcome_from_events,
    parse_task_outcome_from_finish_event,
)


def _finish_event(arguments: dict, *, timestamp: str = "2026-08-12T21:00:00Z"):
    return {
        "kind": "ActionEvent",
        "timestamp": timestamp,
        "tool_name": "finish",
        "tool_call": {
            "id": "call_1",
            "name": "finish",
            "arguments": json.dumps(arguments),
            "origin": "completion",
        },
    }


def test_parse_task_outcome_from_finish_event():
    outcome = parse_task_outcome_from_finish_event(
        _finish_event(
            {
                "message": "Done",
                "summary": "finishing task",
                "status": "partial_success",
                "outcome_summary": "Completed the main work but missed one item.",
                "blockers": [
                    {
                        "type": "external_service",
                        "message": "The reporting API timed out.",
                        "recoverable": True,
                    }
                ],
                "confidence": 0.8,
                "needs_user_action": True,
                "terminal_reason": "finish_action",
            }
        )
    )

    assert outcome is not None
    assert outcome.status == "partial_success"
    assert outcome.summary == "Completed the main work but missed one item."
    assert outcome.blockers[0].type == "external_service"
    assert outcome.confidence == 0.8
    assert outcome.needs_user_action is True
    assert outcome.reported_at == datetime(2026, 8, 12, 21, tzinfo=UTC)
    assert outcome.terminal_reason == "finish_action"


def test_parse_task_outcome_ignores_legacy_finish_without_structured_fields():
    outcome = parse_task_outcome_from_finish_event(
        _finish_event({"message": "Done", "summary": "finishing task"})
    )

    assert outcome is None


def test_latest_task_outcome_uses_latest_finish_event_only():
    non_finish_event = {
        "tool_name": "think",
        "tool_call": {"arguments": json.dumps({"thought": "newer"})},
    }
    structured_event = _finish_event(
        {
            "message": "Done",
            "status": "success",
            "outcome_summary": "Everything completed.",
        },
        timestamp="2026-08-12T21:01:00Z",
    )

    outcome = latest_task_outcome_from_events([non_finish_event, structured_event])

    assert outcome is not None
    assert outcome.summary == "Everything completed."


def test_latest_task_outcome_does_not_fall_back_past_latest_finish():
    legacy_event = _finish_event({"message": "Legacy"})
    older_structured_event = _finish_event(
        {
            "message": "Done",
            "status": "success",
            "outcome_summary": "Everything completed.",
        },
        timestamp="2026-08-12T20:59:00Z",
    )

    assert (
        latest_task_outcome_from_events([legacy_event, older_structured_event]) is None
    )


@pytest.mark.asyncio
async def test_fetch_latest_task_outcome_queries_conversation_events():
    event = _finish_event(
        {
            "message": "Done",
            "status": "success",
            "outcome_summary": "Everything completed.",
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/conv-123/events/search"
        assert request.url.params["kind"] == ACTION_EVENT_KIND
        assert request.url.params["sort_order"] == "TIMESTAMP_DESC"
        assert request.url.params["limit"] == "100"
        assert request.headers["X-Session-API-Key"] == "session-key"
        return httpx.Response(200, json={"items": [event]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await fetch_latest_task_outcome(
            client,
            "https://agent.example.com",
            "session-key",
            "conv-123",
            reported_at=datetime(2026, 8, 12, 21, 2, tzinfo=UTC),
        )

    assert outcome is not None
    assert outcome.summary == "Everything completed."
    assert outcome.reported_at == datetime(2026, 8, 12, 21, 2, tzinfo=UTC)
