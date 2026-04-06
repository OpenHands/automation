"""
Trigger matching logic for event-based automations.

This module provides a simple interface for matching events against triggers.
The actual matching is delegated to the payload's `matches()` method.

## How It Works

1. Payload classes (e.g., `PullRequestPayload`) have a `matches()` method
2. The trigger defines what to match via `on` (event patterns) and `filters` (source-specific)
3. We call `payload.matches(on=trigger.on, filters=trigger.filters)`

That's it. The payload knows how to match itself.
"""

import fnmatch
import logging
from typing import Any

from pydantic import BaseModel

from automation.event_schemas import NormalizedEvent
from automation.schemas import EventTrigger

logger = logging.getLogger("automation.trigger_matcher")


def matches_trigger(trigger: EventTrigger, event: NormalizedEvent) -> bool:
    """
    Check if an event matches an event trigger.

    Delegates to the payload's `matches()` method for the actual matching.

    Args:
        trigger: The event trigger configuration
        event: The normalized event (must have parsed_payload for full matching)

    Returns:
        True if the event matches the trigger

    Examples:
        >>> trigger = EventTrigger(source="github", on="pull_request.opened")
        >>> # payload.matches(on="pull_request.opened") is called internally
        >>> matches_trigger(trigger, event)
        True
    """
    # Source must match
    if trigger.source != event.source:
        return False

    # If we have a parsed payload with matches(), use it
    if event.parsed_payload is not None and hasattr(event.parsed_payload, "matches"):
        return event.parsed_payload.matches(on=trigger.on, filters=trigger.filters)

    # Fallback: manual matching using normalized fields
    return _fallback_match(trigger, event)


def _fallback_match(trigger: EventTrigger, event: NormalizedEvent) -> bool:
    """
    Fallback matching when parsed_payload is not available.

    Uses the normalized fields for basic event_key matching.
    Only supports event pattern matching, not source-specific filters.
    """
    # Build event_key from normalized fields
    event_key = event.normalized.get("event_key")
    if not event_key:
        # Construct from event_type and action
        event_type = event.event_type
        action = event.action
        event_key = f"{event_type}.{action}" if action else event_type

    # Check event pattern matches
    patterns = trigger.event_patterns
    matched_event = False
    for pattern in patterns:
        if pattern == event_key or fnmatch.fnmatch(event_key, pattern):
            matched_event = True
            break

    if not matched_event:
        return False

    # Basic filter support for fallback (repositories only for backward compat)
    if trigger.filters:
        if "repositories" in trigger.filters:
            repo_name = event.normalized.get("repository.full_name")
            if repo_name is None:
                return False
            matched_repo = False
            for pattern in trigger.filters["repositories"]:
                if pattern == repo_name or fnmatch.fnmatch(repo_name, pattern):
                    matched_repo = True
                    break
            if not matched_repo:
                return False

    return True
