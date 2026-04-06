"""
Custom webhook event schema provider.

Handles custom webhook integrations where users define their own
event structures. The provider does minimal normalization since
we don't know the payload structure ahead of time.
"""

from typing import Any, ClassVar

from pydantic import computed_field

from automation.event_schemas import EventSchemaProvider, WebhookEvent


class CustomWebhookEvent(WebhookEvent):
    """
    Generic event for custom webhooks.

    Custom webhooks have minimal structure requirements.
    The payload is stored as-is and the event_key is derived from
    event_type and optional action fields.
    """

    _source: ClassVar[str] = "custom"

    event_type: str
    action: str | None = None
    payload: dict[str, Any] = {}

    @computed_field
    @property
    def event_key(self) -> str:
        """Event key from event_type and optional action."""
        if self.action:
            return f"{self.event_type}.{self.action}"
        return self.event_type


class CustomEventProvider(EventSchemaProvider):
    """Schema provider for custom webhook events."""

    @property
    def source(self) -> str:
        return "custom"

    def parse(self, event_type: str, payload: dict[str, Any]) -> CustomWebhookEvent:
        """
        Parse a custom webhook payload.

        For custom webhooks, we make minimal assumptions about structure:
        - event_type is passed separately
        - Look for 'action' field in payload
        - Store entire payload for user access

        Args:
            event_type: The event type
            payload: The raw webhook payload

        Returns:
            A CustomWebhookEvent instance
        """
        action = payload.get("action")

        return CustomWebhookEvent(
            event_type=event_type,
            action=action,
            payload=payload,
        )

    def get_supported_event_types(self) -> list[str]:
        # Custom webhooks can have any event type
        return ["*"]
