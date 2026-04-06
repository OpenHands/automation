"""
Trigger matching logic for event-based automations.

This module provides a simple interface for matching events against triggers.
The matching is delegated to the payload's `matches()` method.

## How It Works

1. Payload classes (e.g., `PullRequestPayload`) have a `matches()` method
2. The trigger defines what to match via `on` (event patterns) and `filters`
3. We call `event.matches(on=trigger.on, filters=trigger.filters)`

That's it. The event knows how to match itself.
"""

import logging

from automation.event_schemas import WebhookEvent
from automation.schemas import EventTrigger


logger = logging.getLogger("automation.trigger_matcher")


def matches_trigger(trigger: EventTrigger, event: WebhookEvent) -> bool:
    """
    Check if an event matches an event trigger.

    Delegates to the event's `matches()` method.

    Args:
        trigger: The event trigger configuration
        event: The webhook event

    Returns:
        True if the event matches the trigger

    Examples:
        >>> trigger = EventTrigger(source="github", on="pull_request.opened")
        >>> event = PullRequestPayload.model_validate(raw_payload)
        >>> matches_trigger(trigger, event)
        True
    """
    # Source must match
    if trigger.source != event.source:
        return False

    # Delegate to the event's matches() method
    return event.matches(on=trigger.on, filters=trigger.filters)
