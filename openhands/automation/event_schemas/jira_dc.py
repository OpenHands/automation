"""Jira Data Center webhook event parsing."""

from typing import Any, ClassVar

from pydantic import Field, computed_field

from openhands.automation.event_schemas import WebhookEvent


# The events the OpenHands server subscribes its Jira DC webhook to, so the only
# ones it can forward.
JIRA_DC_EVENT_TYPES: tuple[str, ...] = (
    "comment_created",
    "comment_deleted",
    "comment_updated",
    "jira:issue_created",
    "jira:issue_deleted",
    "jira:issue_updated",
)

# Human-readable names a setup UI can show for each event key, keyed the same
# way as the tuple above.
JIRA_DC_EVENT_TYPE_LABELS: dict[str, str] = {
    "comment_created": "Comment created",
    "comment_deleted": "Comment deleted",
    "comment_updated": "Comment updated",
    "jira:issue_created": "Issue created",
    "jira:issue_deleted": "Issue deleted",
    "jira:issue_updated": "Issue updated",
}


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
