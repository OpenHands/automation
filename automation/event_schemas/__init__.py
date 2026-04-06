"""
Event schema registry for detecting event types and normalizing payloads.

This module provides a maintainable way to:
1. Detect the event type from a raw payload
2. Normalize payloads for trigger matching
3. Support multiple event sources (GitHub, GitLab, Linear, custom webhooks)

Built-in sources (github, gitlab) are registered automatically.
Custom webhooks use the 'custom' provider which passes events through unchanged.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import fnmatch
from typing import Any

from pydantic import BaseModel, computed_field


@dataclass
class NormalizedEvent:
    """
    Result of event normalization.

    Contains both the normalized fields for trigger matching and
    optionally the parsed Pydantic model for type-safe access.
    """

    source: str  # e.g., 'github', 'gitlab', 'linear', 'custom'
    event_type: str  # e.g., 'pull_request', 'push', 'issue'
    action: str | None  # e.g., 'opened', 'closed', 'created'
    normalized: dict[str, Any]  # Flattened fields for trigger matching
    parsed_payload: BaseModel | None = None  # Type-safe parsed payload (if available)


class WebhookEvent(BaseModel):
    """
    Base class for all webhook event payloads across all sources.

    Subclasses are self-identifying and self-matching:
    - `event_key` property returns the event identity (e.g., "pull_request.opened")
    - `matches()` method checks if this event matches trigger conditions

    Each source (GitHub, Linear, etc.) subclasses this and implements
    source-specific filter matching via `_matches_filters()`.
    """

    model_config = {"extra": "ignore"}

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
            filters: Source-specific filter conditions (e.g., repos, teams).

        Returns:
            True if this event matches all conditions.
        """
        # Check event key matches
        if not self._matches_event_key(on):
            return False

        # Check source-specific filters
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


class EventSchemaProvider(ABC):
    """Base class for event schema providers."""

    @property
    @abstractmethod
    def source(self) -> str:
        """The source identifier (e.g., 'github', 'gitlab')."""
        pass

    @abstractmethod
    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent:
        """
        Normalize a payload for trigger matching.

        Args:
            payload: The raw or preprocessed webhook payload

        Returns:
            NormalizedEvent with extracted fields
        """
        pass

    @abstractmethod
    def get_supported_event_types(self) -> list[str]:
        """Return list of supported event types for documentation/validation."""
        pass


# Registry of event schema providers
_PROVIDERS: dict[str, EventSchemaProvider] = {}


def register_provider(provider: EventSchemaProvider) -> None:
    """Register an event schema provider."""
    _PROVIDERS[provider.source] = provider


def get_provider(source: str) -> EventSchemaProvider | None:
    """Get the schema provider for a source."""
    return _PROVIDERS.get(source)


def get_all_providers() -> dict[str, EventSchemaProvider]:
    """Get all registered providers."""
    return _PROVIDERS.copy()


# Import and register built-in providers
from automation.event_schemas.github import GitHubEventProvider
from automation.event_schemas.custom import CustomEventProvider

register_provider(GitHubEventProvider())
register_provider(CustomEventProvider())
