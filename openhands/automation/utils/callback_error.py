"""Helpers for SDK automation completion callback errors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from openhands.sdk.event.conversation_error import ConversationErrorEvent


CallbackError = str | ConversationErrorEvent | Mapping[str, Any]


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def parse_callback_error_event(error: CallbackError) -> ConversationErrorEvent | None:
    """Return a typed SDK conversation error when the callback payload matches one."""
    if isinstance(error, ConversationErrorEvent):
        return error
    if not isinstance(error, Mapping):
        return None
    try:
        return ConversationErrorEvent.model_validate(error)
    except ValidationError:
        return None


def format_callback_error(error: CallbackError) -> str:
    """Format legacy string or structured SDK callback errors for persistence."""
    if isinstance(error, str):
        return error

    typed_error = parse_callback_error_event(error)
    if typed_error is not None:
        detail = typed_error.detail
        code = typed_error.code
        source = typed_error.source
        kind = (
            typed_error.classification.kind.value
            if typed_error.classification
            else None
        )
    else:
        assert isinstance(error, Mapping)
        detail = _string_value(error.get("detail")) or _string_value(
            error.get("message")
        )
        code = _string_value(error.get("code"))
        source = _string_value(error.get("source"))

        classification = error.get("classification")
        kind = None
        if isinstance(classification, Mapping):
            kind = _string_value(classification.get("kind"))

    if code and detail:
        formatted = f"{code}: {detail}"
    else:
        formatted = detail or code or "Structured callback error"

    metadata = []
    if kind:
        metadata.append(f"kind={kind}")
    if source:
        metadata.append(f"source={source}")
    if metadata:
        formatted += f" [{', '.join(metadata)}]"

    return formatted
