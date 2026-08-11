"""
Custom webhook event for user-defined webhook integrations.

Custom webhooks have minimal structure requirements - the payload
is stored as-is and users define how to extract the event_key using JMESPath.

Example event_key_expr values:
- "type"                           # Simple field access
- "event.type"                     # Nested field
- "type || event.type"             # Fallback: try type, then event.type
- "join('.', [category, action])"  # Concatenate fields
"""

from typing import Any, ClassVar

import jmespath
from jmespath import exceptions as jmespath_exceptions
from pydantic import PrivateAttr, computed_field

from openhands.automation.event_schemas import WebhookEvent


def extract_event_key(payload: dict[str, Any], expr: str) -> str:
    """
    Extract event key from payload using a JMESPath expression.

    Args:
        payload: The webhook payload dict
        expr: JMESPath expression that should return the event key string

    Returns:
        The extracted event key as a string

    Raises:
        ValueError: If expression fails or returns non-string/null

    Examples:
        >>> extract_event_key({"type": "payment.completed"}, "type")
        "payment.completed"

        >>> extract_event_key({"event": {"name": "order"}}, "event.name")
        "order"

        >>> extract_event_key({"a": "x", "b": "y"}, "a || b")
        "x"

        >>> extract_event_key({"foo": "bar"}, "type")
        ValueError: Could not extract event_key...
    """
    try:
        result = jmespath.search(expr, payload)
    except jmespath_exceptions.JMESPathError as e:
        raise ValueError(f"Invalid event_key expression '{expr}': {e}") from e

    if result is None:
        keys = list(payload.keys())
        raise ValueError(
            f"Could not extract event_key from payload using expression '{expr}'. "
            f"Expression returned null. Available top-level keys: {keys}"
        )

    if not isinstance(result, str):
        # Try to convert to string if it's a simple type
        if isinstance(result, (int, float, bool)):
            return str(result)
        raise ValueError(
            f"Event key expression '{expr}' returned {type(result).__name__}, "
            f"expected string. Value: {result}"
        )

    return result


class CustomWebhookEvent(WebhookEvent):
    """
    Generic event for custom webhooks.

    The event_key is extracted from the payload using a configurable path.
    The source is set dynamically based on the actual source name from the URL.
    """

    _source: ClassVar[str] = "custom"  # Default, overridden per-instance

    # The extracted event identifier (e.g., "payment.completed", "order.created")
    # Using PrivateAttr since this is set at construction, not from payload
    _event_key: str = PrivateAttr()

    # The raw payload for user access
    payload: dict[str, Any] = {}  # noqa: RUF012

    # Dynamic source name (e.g., "stripe", "my-webhook")
    source_override: str | None = None

    def __init__(self, _event_key: str, **data: Any) -> None:
        """Initialize with the extracted event key."""
        super().__init__(**data)
        self._event_key = _event_key

    @property
    def source(self) -> str:
        """Return the actual source name (e.g., 'stripe', 'my-webhook')."""
        return self.source_override or self._source

    @computed_field
    @property
    def event_key(self) -> str:
        """The event identifier extracted from the payload."""
        return self._event_key
