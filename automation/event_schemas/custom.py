"""
Custom webhook event for user-defined webhook integrations.

Custom webhooks have minimal structure requirements - the payload
is stored as-is and users can match on event_type and action.
"""

from typing import Any, ClassVar

from pydantic import computed_field

from automation.event_schemas import WebhookEvent


class CustomWebhookEvent(WebhookEvent):
    """
    Generic event for custom webhooks.

    Custom webhooks have minimal structure requirements.
    The payload is stored as-is and the event_key is derived from
    event_type and optional action fields.

    The `_source` is set dynamically based on the actual source name
    from the webhook URL, not a fixed "custom" value.
    """

    _source: ClassVar[str] = "custom"  # Default, but overridden per-instance

    event_type: str
    action: str | None = None
    payload: dict[str, Any] = {}

    # Allow overriding source per-instance for custom webhooks
    source_override: str | None = None

    @property
    def source(self) -> str:
        """Return the actual source name (e.g., 'stripe', 'my-webhook')."""
        return self.source_override or self._source

    @computed_field
    @property
    def event_key(self) -> str:
        """Event key from event_type and optional action."""
        if self.action:
            return f"{self.event_type}.{self.action}"
        return self.event_type
