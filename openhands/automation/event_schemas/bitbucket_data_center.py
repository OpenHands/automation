"""Bitbucket Data Center webhook event parsing."""

from typing import Any, ClassVar

from pydantic import Field, computed_field

from openhands.automation.event_schemas import WebhookEvent


# The events the OpenHands server subscribes its Bitbucket Data Center webhooks
# to, so the only ones it can forward.
BITBUCKET_DATA_CENTER_EVENT_TYPES: tuple[str, ...] = (
    "pr:comment:added",
    "pr:comment:deleted",
    "pr:comment:edited",
    "pr:declined",
    "pr:deleted",
    "pr:from_ref_updated",
    "pr:merged",
    "pr:modified",
    "pr:opened",
    "pr:reviewer:approved",
    "pr:reviewer:needs_work",
    "pr:reviewer:unapproved",
    "repo:comment:added",
    "repo:comment:deleted",
    "repo:comment:edited",
    "repo:refs_changed",
)


class BitbucketDataCenterEvent(WebhookEvent):
    """Bitbucket Data Center webhook event.

    Bitbucket Data Center exposes the event identity in the top-level
    ``eventKey`` field. The raw payload is preserved for automation code and
    filtering.
    """

    _source: ClassVar[str] = "bitbucket_data_center"

    event_key_value: str = Field(alias="eventKey")
    payload: dict[str, Any]

    @computed_field
    @property
    def event_key(self) -> str:
        """Return the Bitbucket Data Center webhook event key."""
        return self.event_key_value


def parse_bitbucket_data_center_event(
    payload: dict[str, Any],
) -> BitbucketDataCenterEvent:
    """Parse a Bitbucket Data Center webhook payload."""
    event_key = payload.get("eventKey")
    if not isinstance(event_key, str) or not event_key:
        raise ValueError("Cannot detect bitbucket_data_center event type")

    return BitbucketDataCenterEvent(eventKey=event_key, payload=payload)
