"""Jira Data Center webhook event parsing."""

from typing import Any, ClassVar

from pydantic import Field, computed_field

from openhands.automation.event_schemas import WebhookEvent


class JiraDcEvent(WebhookEvent):
    """Jira Data Center webhook event.

    Jira DC exposes the event identity in the top-level ``webhookEvent`` field.
    The raw payload is preserved for automation code and filtering.
    """

    _source: ClassVar[str] = "jira_dc"

    webhook_event: str = Field(alias="webhookEvent")
    payload: dict[str, Any]

    @computed_field
    @property
    def event_key(self) -> str:
        """Return the Jira DC webhook event key."""
        return self.webhook_event


def parse_jira_dc_event(payload: dict[str, Any]) -> JiraDcEvent:
    """Parse a Jira DC webhook payload."""
    event_key = payload.get("webhookEvent")
    if not isinstance(event_key, str) or not event_key:
        raise ValueError("Cannot detect jira_dc event type")

    return JiraDcEvent(webhookEvent=event_key, payload=payload)
