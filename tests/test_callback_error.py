"""Tests for SDK callback error formatting."""

from openhands.automation.utils.callback_error import format_callback_error


def test_format_callback_error_preserves_legacy_string():
    assert format_callback_error("script crashed") == "script crashed"


def test_format_callback_error_formats_conversation_error_event():
    error = {
        "source": "environment",
        "code": "RuntimeError",
        "detail": "script crashed",
        "classification": {"kind": "unknown"},
    }

    assert (
        format_callback_error(error)
        == "RuntimeError: script crashed [kind=unknown, source=environment]"
    )


def test_format_callback_error_handles_minimal_structured_error():
    assert format_callback_error({"detail": "bad config"}) == "bad config"
