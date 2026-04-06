"""
Custom webhook event schema provider.

Handles custom webhook integrations where users define their own
event structures. The provider does minimal normalization since
we don't know the payload structure ahead of time.
"""

from typing import Any

from automation.event_schemas import EventSchemaProvider, NormalizedEvent


class CustomEventProvider(EventSchemaProvider):
    """Schema provider for custom webhook events."""

    @property
    def source(self) -> str:
        return "custom"

    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent:
        """
        Normalize a custom webhook payload.

        For custom webhooks, we make minimal assumptions about structure:
        - Look for common fields: type, event_type, action, event
        - Flatten top-level fields for matching
        """
        # Try to extract event type from common field names
        event_type = (
            payload.get("type")
            or payload.get("event_type")
            or payload.get("event")
            or "custom"
        )

        # Try to extract action from common field names
        action = payload.get("action")

        # Flatten top-level fields for trigger matching
        normalized: dict[str, Any] = {
            "event_type": event_type,
            "action": action,
        }

        # Include all top-level string/int/bool fields
        for key, value in payload.items():
            if isinstance(value, (str, int, bool, float)):
                normalized[key] = value

        return NormalizedEvent(
            source=self.source,
            event_type=str(event_type),
            action=action,
            normalized=normalized,
        )

    def get_supported_event_types(self) -> list[str]:
        # Custom webhooks can have any event type
        return ["*"]
