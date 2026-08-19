"""Tests for SDK callback error formatting."""

from openhands.automation.utils.callback_error import (
    format_callback_error,
    parse_callback_error_event,
)


def test_format_callback_error_preserves_legacy_string():
    assert format_callback_error("script crashed") == "script crashed"


def test_format_callback_error_formats_conversation_error_event():
    error = {
        "source": "environment",
        "code": "RuntimeError",
        "detail": "script crashed",
        "classification": {"kind": "unknown", "retryable": False},
    }

    assert (
        format_callback_error(error)
        == "RuntimeError: script crashed [kind=unknown, source=environment]"
    )


def test_parse_callback_error_event_uses_sdk_model():
    error = {
        "source": "environment",
        "code": "LLMAuthenticationError",
        "detail": "incorrect API key",
        "classification": {
            "kind": "auth",
            "retryable": False,
            "user_action": "settings",
        },
    }

    parsed = parse_callback_error_event(error)

    assert parsed is not None
    assert parsed.code == "LLMAuthenticationError"
    assert parsed.detail == "incorrect API key"
    assert parsed.classification is not None
    assert parsed.classification.kind.value == "auth"
    assert parsed.classification.user_action == "settings"


def test_format_callback_error_handles_minimal_structured_error():
    assert format_callback_error({"detail": "bad config"}) == "bad config"
