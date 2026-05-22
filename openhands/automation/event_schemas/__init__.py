"""
Event schema module for webhook event processing.

This module provides:
1. `WebhookEvent` base class for typed event payloads
2. `parse_event()` function to parse payloads from any source

Each source (GitHub, Linear, etc.) has its own WebhookEvent subclass.
Unknown sources automatically get `CustomWebhookEvent`.

Note: Filtering is handled by the trigger_matcher module using JMESPath
expressions against the raw payload. The event schemas are for validation
and providing typed access to payload fields.
"""

from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel, computed_field


class WebhookEvent(BaseModel):
    """
    Base class for all webhook event payloads across all sources.

    Subclasses are self-identifying:
    - `source` property returns the event source (e.g., 'github')
    - `event_key` property returns the event identity (e.g., "pull_request.opened")

    Filtering is handled externally by the trigger_matcher module using
    JMESPath expressions evaluated against the raw payload.
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


# =============================================================================
# Parser Registry
# =============================================================================

# Type for auto-detection parse functions: (payload) -> WebhookEvent
AutoParseFunc = Callable[[dict[str, Any]], WebhookEvent]

# Registry of parse functions for known sources (auto-detect event type from payload)
_PARSERS: dict[str, AutoParseFunc] = {}


def register_parser(source: str, parser: AutoParseFunc) -> None:
    """Register a parse function for a source."""
    _PARSERS[source] = parser


def parse_event(
    source: str,
    payload: dict[str, Any],
    *,
    event_key_expr: str | None = None,
) -> WebhookEvent:
    """
    Parse a webhook payload into a typed WebhookEvent.

    For known sources (github, linear, etc.), auto-detects event type from
    payload structure and returns a typed event. For unknown sources (custom
    webhooks), returns a CustomWebhookEvent.

    Args:
        source: The event source (e.g., 'github', 'stripe', 'my-webhook')
        payload: The raw webhook payload
        event_key_expr: JMESPath expression for extracting event_key from payload
                        (used for custom webhooks, default: "type")

    Returns:
        A WebhookEvent subclass instance

    Raises:
        ValueError: If event type cannot be determined from payload
    """
    # Known source - auto-detect event type from payload
    parser = _PARSERS.get(source)
    if parser:
        return parser(payload)

    # Unknown source = custom webhook (no registration needed)
    from openhands.automation.event_schemas.custom import (
        CustomWebhookEvent,
        extract_event_key,
    )

    # Extract event_key using JMESPath expression
    expr = event_key_expr or "type"
    event_key = extract_event_key(payload, expr)

    return CustomWebhookEvent(
        _event_key=event_key,
        payload=payload,
        source_override=source,
    )


def _register_builtin_parsers() -> None:
    """Register parsers for built-in sources. Called at module load."""
    from openhands.automation.event_schemas.github import parse_github_event_auto
    from openhands.automation.event_schemas.jira_dc import parse_jira_dc_event

    register_parser("github", parse_github_event_auto)
    register_parser("jira_dc", parse_jira_dc_event)


_register_builtin_parsers()
