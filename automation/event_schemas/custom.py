"""
Custom webhook event for user-defined webhook integrations.

Custom webhooks have minimal structure requirements - the payload
is stored as-is and users define how to extract the event_key.
"""

from typing import Any, ClassVar

from pydantic import computed_field

from automation.event_schemas import WebhookEvent


def extract_by_path(payload: dict[str, Any], path: str) -> str | None:
    """
    Extract a value from a nested dict using dot-notation path.

    Args:
        payload: The dict to extract from
        path: Dot-notation path (e.g., "type", "event.name", "data.event_type")

    Returns:
        The extracted string value, or None if not found

    Examples:
        >>> extract_by_path({"type": "payment.completed"}, "type")
        "payment.completed"
        >>> extract_by_path({"event": {"name": "order.created"}}, "event.name")
        "order.created"
    """
    value: Any = payload
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return str(value) if value is not None else None


class CustomWebhookEvent(WebhookEvent):
    """
    Generic event for custom webhooks.

    The event_key is extracted from the payload using a configurable path.
    The source is set dynamically based on the actual source name from the URL.
    """

    _source: ClassVar[str] = "custom"  # Default, overridden per-instance

    # The extracted event identifier (e.g., "payment.completed", "order.created")
    _event_key: str

    # The raw payload for user access
    payload: dict[str, Any] = {}

    # Dynamic source name (e.g., "stripe", "my-webhook")
    source_override: str | None = None

    @property
    def source(self) -> str:
        """Return the actual source name (e.g., 'stripe', 'my-webhook')."""
        return self.source_override or self._source

    @computed_field
    @property
    def event_key(self) -> str:
        """The event identifier extracted from the payload."""
        return self._event_key
