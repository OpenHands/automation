"""
JMESPath-based filter evaluation for webhook event matching.

This module provides a DSL for filtering webhook payloads using JMESPath
expressions with custom functions for common matching patterns.

Example filters:
    # Match comments mentioning @openhands-resolver
    "icontains(comment.body, '@openhands-resolver')"

    # Match repos with wildcard
    "glob(repository.full_name, 'OpenHands/*')"

    # Complex conditions
    "glob(repository.full_name, 'org/*') && icontains(comment.body, '@bot')"

    # Match PRs with specific labels
    "contains(pull_request.labels[].name, 'bug')"

Custom functions:
    - glob(str, pattern): Wildcard matching using fnmatch
    - icontains(str, substr): Case-insensitive substring match
    - regex(str, pattern): Regular expression match
    - lower(str): Convert to lowercase
"""

import fnmatch
import logging
import re
from typing import Any

import jmespath
from jmespath import exceptions as jmespath_exceptions, functions


logger = logging.getLogger(__name__)


class FilterFunctions(functions.Functions):
    """
    Custom JMESPath functions for webhook payload filtering.

    These extend the built-in JMESPath functions with common patterns
    needed for event matching.
    """

    @functions.signature({"types": ["string"]}, {"types": ["string"]})
    def _func_glob(self, subject: str, pattern: str) -> bool:
        """
        Wildcard/glob pattern matching using fnmatch.

        Args:
            subject: The string to match against
            pattern: Glob pattern (supports *, ?, [seq], [!seq])

        Returns:
            True if subject matches pattern

        Examples:
            glob(repository.full_name, 'OpenHands/*') -> True for 'OpenHands/repo'
            glob(ref, 'refs/heads/release-*') -> True for 'refs/heads/release-1.0'
        """
        return fnmatch.fnmatch(subject, pattern)

    @functions.signature({"types": ["string"]}, {"types": ["string"]})
    def _func_icontains(self, subject: str, substring: str) -> bool:
        """
        Case-insensitive substring match.

        Args:
            subject: The string to search in
            substring: The substring to find

        Returns:
            True if substring is found (case-insensitive)

        Examples:
            icontains(comment.body, '@openhands-resolver')
            -> True for '@OpenHands-Resolver'
        """
        return substring.lower() in subject.lower()

    @functions.signature({"types": ["string"]}, {"types": ["string"]})
    def _func_regex(self, subject: str, pattern: str) -> bool:
        """
        Regular expression match.

        Args:
            subject: The string to match against
            pattern: Regular expression pattern

        Returns:
            True if pattern matches anywhere in subject

        Examples:
            regex(ref, '^refs/tags/v\\d+') -> True for 'refs/tags/v1.0.0'
        """
        try:
            return bool(re.search(pattern, subject))
        except re.error as e:
            logger.warning("Invalid regex pattern '%s': %s", pattern, e)
            return False

    @functions.signature({"types": ["string"]})
    def _func_lower(self, subject: str) -> str:
        """
        Convert string to lowercase.

        Useful for case-insensitive comparisons in complex expressions.

        Examples:
            lower(sender.login) == 'admin'
        """
        return subject.lower()

    @functions.signature({"types": ["string"]})
    def _func_upper(self, subject: str) -> str:
        """Convert string to uppercase."""
        return subject.upper()

    @functions.signature({"types": ["string"]}, {"types": ["string"]})
    def _func_starts_with(self, subject: str, prefix: str) -> bool:
        """Check if string starts with prefix."""
        return subject.startswith(prefix)

    @functions.signature({"types": ["string"]}, {"types": ["string"]})
    def _func_ends_with(self, subject: str, suffix: str) -> bool:
        """Check if string ends with suffix."""
        return subject.endswith(suffix)


# Singleton options instance with custom functions
_FILTER_OPTIONS = jmespath.Options(custom_functions=FilterFunctions())


def evaluate_filter(expression: str, payload: dict[str, Any]) -> bool:
    """
    Evaluate a JMESPath filter expression against a payload.

    Args:
        expression: JMESPath expression that should evaluate to a boolean
        payload: The webhook payload to filter

    Returns:
        True if the expression evaluates to a truthy value, False otherwise

    Raises:
        FilterEvaluationError: If the expression is invalid or evaluation fails

    Examples:
        >>> payload = {"comment": {"body": "Fix @openhands-resolver"}}
        >>> evaluate_filter("icontains(comment.body, '@openhands')", payload)
        True
    """
    try:
        result = jmespath.search(expression, payload, options=_FILTER_OPTIONS)
        # Convert result to boolean
        return bool(result)
    except jmespath_exceptions.JMESPathError as e:
        raise FilterEvaluationError(f"Invalid filter expression: {e}") from e
    except Exception as e:
        raise FilterEvaluationError(f"Filter evaluation failed: {e}") from e


def validate_filter(expression: str) -> tuple[bool, str | None]:
    """
    Validate a JMESPath filter expression without evaluating it.

    Args:
        expression: JMESPath expression to validate

    Returns:
        Tuple of (is_valid, error_message)

    Examples:
        >>> validate_filter("contains(body, 'test')")
        (True, None)
        >>> validate_filter("invalid(((")
        (False, "Incomplete expression...")
    """
    try:
        # Compile the expression to check syntax
        jmespath.compile(expression)
        return True, None
    except jmespath_exceptions.JMESPathError as e:
        return False, str(e)


class FilterEvaluationError(Exception):
    """Raised when filter evaluation fails."""

    pass
