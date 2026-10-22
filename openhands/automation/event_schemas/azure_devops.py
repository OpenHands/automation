"""Azure DevOps Service Hook event parsing."""

from typing import Any, ClassVar

from pydantic import Field, computed_field

from openhands.automation.event_schemas import WebhookEvent


class AzureDevOpsEvent(WebhookEvent):
    """Azure DevOps Service Hook event.

    Azure DevOps exposes the event identity in the top-level ``eventType`` field,
    e.g. ``git.pullrequest.created``. The raw payload is preserved for
    automation code and filtering.
    """

    _source: ClassVar[str] = "azure_devops"

    event_type: str = Field(alias="eventType")
    payload: dict[str, Any]

    @computed_field
    @property
    def event_key(self) -> str:
        """Return the Azure DevOps Service Hook event type."""
        return self.event_type


def parse_azure_devops_event(payload: dict[str, Any]) -> AzureDevOpsEvent:
    """Parse an Azure DevOps Service Hook payload."""
    event_type = payload.get("eventType")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("Cannot detect azure_devops event type")

    return AzureDevOpsEvent(eventType=event_type, payload=payload)
