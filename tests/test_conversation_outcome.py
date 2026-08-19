import json

import httpx
import pytest

from openhands.automation.utils.conversation_outcome import (
    ACTION_EVENT_KIND,
    fetch_latest_finish_tool_response,
    finish_tool_response_from_event,
    latest_finish_tool_response_from_events,
)


def _finish_event(arguments: dict | str, *, timestamp: str = "2026-08-12T21:00:00Z"):
    raw_arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "kind": "ActionEvent",
        "timestamp": timestamp,
        "tool_name": "finish",
        "tool_call": {
            "id": "call_1",
            "name": "finish",
            "arguments": raw_arguments,
            "origin": "completion",
        },
    }


def test_finish_tool_response_from_event_returns_raw_arguments():
    arguments = {
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

    assert finish_tool_response_from_event(_finish_event(arguments)) == arguments


def test_finish_tool_response_keeps_legacy_finish_arguments():
    arguments = {"message": "Done", "summary": "finishing task"}

    assert finish_tool_response_from_event(_finish_event(arguments)) == arguments


def test_finish_tool_response_keeps_invalid_json_as_raw_string():
    assert finish_tool_response_from_event(_finish_event("not-json")) == "not-json"


def test_latest_finish_tool_response_uses_latest_finish_event_only():
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

    assert latest_finish_tool_response_from_events(
        [non_finish_event, structured_event]
    ) == {
        "message": "Done",
        "status": "success",
        "outcome_summary": "Everything completed.",
    }


def test_latest_finish_tool_response_does_not_fall_back_past_latest_finish():
    legacy_event = _finish_event({"message": "Legacy"})
    older_structured_event = _finish_event(
        {
            "message": "Done",
            "status": "success",
            "outcome_summary": "Everything completed.",
        },
        timestamp="2026-08-12T20:59:00Z",
    )

    assert latest_finish_tool_response_from_events(
        [legacy_event, older_structured_event]
    ) == {"message": "Legacy"}


@pytest.mark.asyncio
async def test_fetch_latest_finish_tool_response_queries_conversation_events():
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
        response = await fetch_latest_finish_tool_response(
            client,
            "https://agent.example.com",
            "session-key",
            "conv-123",
        )

    assert response == {
        "message": "Done",
        "status": "success",
        "outcome_summary": "Everything completed.",
    }
