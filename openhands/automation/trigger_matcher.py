"""
Trigger matching logic for event-based automations.

This module provides the interface for matching events against triggers.

## How It Works

1. Match event source (github, linear, etc.)
2. Match event key pattern (e.g., "pull_request.opened", "push")
3. Evaluate JMESPath filter expression against the webhook payload

The JMESPath filter enables powerful, decoupled filtering without
source-specific code. Any JSON path in the payload can be matched.
"""

import fnmatch
import logging
from typing import Any

from openhands.automation.filter_eval import FilterEvaluationError, evaluate_filter
from openhands.automation.schemas import EventTrigger


logger = logging.getLogger("automation.trigger_matcher")


def match_trigger_with_reason(
    trigger: EventTrigger,
    event_source: str,
    event_key: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """
    Check if an event matches an event trigger.

    Args:
        trigger: The event trigger configuration
        event_source: Source of the event (e.g., 'github', 'linear')
        event_key: Event key (e.g., 'pull_request.opened', 'push')
        payload: The webhook payload for filter evaluation

    Returns:
        Tuple of whether the trigger matched and a diagnostic reason.

    Examples:
        >>> trigger = EventTrigger(
        ...     source="github",
        ...     on="issue_comment.created",
        ...     filter="icontains(comment.body, '@openhands-resolver')"
        ... )
        >>> matches_trigger(trigger, "github", "issue_comment.created", payload)
        True
    """
    # 1. Source must match
    if trigger.source != event_source:
        return False, f"source mismatch: trigger_source={trigger.source}"

    # 2. Event key must match one of the patterns
    if not _matches_event_key(event_key, trigger.on):
        return False, f"event key mismatch: trigger_on={trigger.on}"

    # 3. Filter expression must evaluate to true (if specified)
    if trigger.filter:
        try:
            if not evaluate_filter(trigger.filter, payload):
                logger.debug(
                    "Filter did not match: %s for event %s",
                    trigger.filter,
                    event_key,
                )
                return False, f"filter mismatch: filter={trigger.filter}"
        except FilterEvaluationError as e:
            logger.warning("Filter evaluation failed: %s", e)
            return False, f"filter evaluation error: {e}"

    return True, "matched"


def matches_trigger(
    trigger: EventTrigger,
    event_source: str,
    event_key: str,
    payload: dict[str, Any],
) -> bool:
    """Check if an event matches an event trigger."""
    matched, _reason = match_trigger_with_reason(
        trigger, event_source, event_key, payload
    )
    return matched


def _matches_event_key(event_key: str, on: str | list[str]) -> bool:
    """Check if event_key matches any of the patterns."""
    patterns = [on] if isinstance(on, str) else on

    for pattern in patterns:
        if pattern == event_key:
            return True
        if fnmatch.fnmatch(event_key, pattern):
            return True

    return False
