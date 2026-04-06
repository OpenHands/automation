"""
Event schema module for webhook event processing.

This module provides:
1. `WebhookEvent` base class for self-matching event payloads
2. `parse_event()` function to parse payloads from any source

Each source (GitHub, Linear, etc.) has its own WebhookEvent subclass.
Unknown sources automatically get `CustomWebhookEvent`.
"""

import fnmatch
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, computed_field


class WebhookEvent(BaseModel):
    """
    Base class for all webhook event payloads across all sources.

    Subclasses are self-identifying and self-matching:
    - `event_key` property returns the event identity (e.g., "pull_request.opened")
    - `matches()` method checks if this event matches trigger conditions

    Each source (GitHub, Linear, etc.) subclasses this and implements
    source-specific filter matching via `_matches_filters()`.
    """

    # Subclasses should define their source
    _source: ClassVar[str] = "unknown"

    model_config = {"extra": "ignore"}

    @property
    def source(self) -> str:
        """The event source (e.g., 'github', 'linear')."""
        return self._source

    @computed_field
    @property
    def event_key(self) -> str:
        """
        Unique identifier for this event instance.

        Format: "{event_type}.{action}" or "{event_type}" if no action.
        Must be implemented by subclasses.
        """
        raise NotImplementedError("Subclasses must implement event_key")

    def matches(
        self,
        on: str | list[str],
        filters: dict[str, list[str]] | None = None,
    ) -> bool:
        """
        Check if this event matches the trigger conditions.

        Args:
            on: Event key pattern(s) to match. Supports wildcards via fnmatch.
            filters: Source-specific filter conditions (e.g., repositories, teams).

        Returns:
            True if this event matches all conditions.
        """
        if not self._matches_event_key(on):
            return False

        if filters and not self._matches_filters(filters):
            return False

        return True

    def _matches_event_key(self, on: str | list[str]) -> bool:
        """Check if event_key matches any of the patterns."""
        patterns = [on] if isinstance(on, str) else on
        event_key = self.event_key

        for pattern in patterns:
            if pattern == event_key:
                return True
            if fnmatch.fnmatch(event_key, pattern):
                return True

        return False

    def _matches_filters(self, filters: dict[str, list[str]]) -> bool:
        """
        Check if event matches source-specific filters.

        Subclasses override this to implement their filter logic.
        Default implementation returns True (no filtering).
        """
        return True

    @staticmethod
    def _filter_matches(value: str | None, patterns: list[str]) -> bool:
        """
        Helper to check if a value matches any of the filter patterns.

        Supports exact match and wildcards via fnmatch.
        """
        if value is None:
            return False
        for pattern in patterns:
            if pattern == value or fnmatch.fnmatch(value, pattern):
                return True
        return False


# =============================================================================
# Parser Registry
# =============================================================================

# Type for parse functions
ParseFunc = Callable[[str, dict[str, Any]], WebhookEvent]

# Registry of parse functions for known sources
_PARSERS: dict[str, ParseFunc] = {}


def register_parser(source: str, parser: ParseFunc) -> None:
    """Register a parse function for a source."""
    _PARSERS[source] = parser


def parse_event(source: str, event_type: str, payload: dict[str, Any]) -> WebhookEvent:
    """
    Parse a webhook payload into a typed WebhookEvent.

    For known sources (github, linear, etc.), uses the registered parser.
    For unknown sources (custom webhooks), returns a CustomWebhookEvent.

    Args:
        source: The event source (e.g., 'github', 'stripe', 'my-webhook')
        event_type: The event type (e.g., 'pull_request', 'payment')
        payload: The raw webhook payload

    Returns:
        A WebhookEvent subclass instance
    """
    parser = _PARSERS.get(source)
    if parser:
        return parser(event_type, payload)

    # Unknown source = custom webhook (no registration needed)
    from automation.event_schemas.custom import CustomWebhookEvent
    return CustomWebhookEvent(
        event_type=event_type,
        action=payload.get("action"),
        payload=payload,
        source_override=source,  # Pass actual source name
    )


# =============================================================================
# Register Built-in Parsers
# =============================================================================

from automation.event_schemas.github import parse_github_event

register_parser("github", parse_github_event)
