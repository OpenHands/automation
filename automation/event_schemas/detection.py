"""
Event type detection using JMESPath expressions.

This module provides declarative, data-driven event type detection.
Detection rules are defined as (event_type, jmespath_expr) tuples,
evaluated in order. The first matching rule determines the event type.

Example:
    >>> from automation.event_schemas.detection import EventTypeDetector
    >>> detector = EventTypeDetector([
    ...     ("pull_request_review", "pull_request && review"),
    ...     ("pull_request", "pull_request"),
    ...     ("push", "ref && commits"),
    ... ])
    >>> detector.detect({"pull_request": {...}, "review": {...}})
    'pull_request_review'

Design Principles:
    1. Rules are data, not code - easy to read, modify, and test
    2. JMESPath expressions are pre-compiled at init for efficiency
    3. Order matters: more specific patterns should come first
    4. Reuses existing JMESPath infrastructure from filter_eval
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jmespath
from jmespath import exceptions as jmespath_exceptions


if TYPE_CHECKING:
    from jmespath.parser import ParsedResult


@dataclass(frozen=True, slots=True)
class DetectionRule:
    """
    A compiled detection rule.

    Attributes:
        event_type: The event type to return if this rule matches
        expression: The original JMESPath expression string
        compiled: Pre-compiled JMESPath expression for efficient evaluation
    """

    event_type: str
    expression: str
    compiled: ParsedResult


class EventTypeDetector:
    """
    Detects event type from payload structure using JMESPath rules.

    Rules are evaluated in order; the first truthy match wins.
    Expressions are pre-compiled at initialization for O(1) evaluation.

    Usage:
        detector = EventTypeDetector([
            ("specific_event", "key1 && key2"),
            ("general_event", "key1"),
        ])
        event_type = detector.detect(payload)
    """

    __slots__ = ("_rules", "_source")

    def __init__(
        self,
        rules: list[tuple[str, str]],
        source: str = "unknown",
    ) -> None:
        """
        Initialize detector with detection rules.

        Args:
            rules: List of (event_type, jmespath_expression) tuples.
                   Order matters - more specific patterns should come first.
            source: Source name for error messages (e.g., "github")

        Raises:
            ValueError: If a rule expression is invalid
        """
        self._source = source
        self._rules: tuple[DetectionRule, ...] = tuple(
            self._compile_rule(event_type, expr) for event_type, expr in rules
        )

    def _compile_rule(self, event_type: str, expression: str) -> DetectionRule:
        """Compile a single rule, raising ValueError on invalid expression."""
        try:
            compiled = jmespath.compile(expression)
            return DetectionRule(
                event_type=event_type,
                expression=expression,
                compiled=compiled,
            )
        except jmespath_exceptions.JMESPathError as e:
            raise ValueError(
                f"Invalid detection rule for '{event_type}': {expression!r} - {e}"
            ) from e

    def detect(self, payload: dict[str, Any]) -> str:
        """
        Detect event type from payload.

        Args:
            payload: The raw webhook payload

        Returns:
            The detected event type string

        Raises:
            ValueError: If no rule matches the payload
        """
        for rule in self._rules:
            if rule.compiled.search(payload):
                return rule.event_type

        raise ValueError(
            f"Cannot detect {self._source} event type from payload. "
            f"Top-level keys: {list(payload.keys())[:10]}"
        )

    def detect_or_none(self, payload: dict[str, Any]) -> str | None:
        """
        Detect event type from payload, returning None if no match.

        Args:
            payload: The raw webhook payload

        Returns:
            The detected event type string, or None if no rule matches
        """
        for rule in self._rules:
            if rule.compiled.search(payload):
                return rule.event_type
        return None

    @property
    def supported_types(self) -> list[str]:
        """Get list of event types this detector can identify."""
        return [rule.event_type for rule in self._rules]

    @property
    def rules(self) -> tuple[DetectionRule, ...]:
        """Get the compiled detection rules (for introspection/debugging)."""
        return self._rules
