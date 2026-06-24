"""Automation timeout policy helpers."""

from openhands.automation.config import get_config


def get_default_automation_timeout_seconds() -> int:
    """Return the configured default run timeout in seconds."""
    return get_config().sandbox.max_run_duration


def get_max_automation_timeout_seconds() -> int:
    """Return the configured maximum user-provided run timeout in seconds."""
    return get_config().sandbox.max_automation_timeout


def build_automation_timeout_description(*, include_default: bool) -> str:
    """Return a timeout field description using configured timeout values."""
    max_timeout = get_max_automation_timeout_seconds()
    if include_default:
        default_timeout = get_default_automation_timeout_seconds()
        return (
            "Maximum execution time in seconds "
            f"(default: {default_timeout} seconds, maximum: {max_timeout} seconds)"
        )
    return f"Maximum execution time in seconds (maximum: {max_timeout} seconds)"


def validate_automation_timeout(timeout: int | None) -> int | None:
    """Validate a user-provided automation timeout in seconds."""
    if timeout is None:
        return timeout
    if timeout <= 0:
        raise ValueError("timeout must be a positive number")

    max_timeout = get_max_automation_timeout_seconds()
    if timeout > max_timeout:
        max_minutes = max_timeout // 60
        raise ValueError(
            f"timeout must not exceed {max_timeout} seconds ({max_minutes} minutes)"
        )
    return timeout


def resolve_automation_timeout_seconds(timeout: int | None) -> int:
    """Return the effective run timeout in seconds.

    ``None`` preserves the configured service default, while explicit user values
    are capped by the configured public API limit as a defense in depth.
    """
    effective_timeout = (
        timeout if timeout is not None else get_default_automation_timeout_seconds()
    )
    return min(effective_timeout, get_max_automation_timeout_seconds())
