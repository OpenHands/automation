"""Automation timeout policy helpers."""

from openhands.automation.config import get_config


MAX_AUTOMATION_TIMEOUT_SECONDS = 30 * 60
MAX_AUTOMATION_TIMEOUT_MINUTES = MAX_AUTOMATION_TIMEOUT_SECONDS // 60


def validate_automation_timeout(timeout: int | None) -> int | None:
    """Validate a user-provided automation timeout in seconds."""
    if timeout is None:
        return timeout
    if timeout <= 0:
        raise ValueError("timeout must be a positive number")
    if timeout > MAX_AUTOMATION_TIMEOUT_SECONDS:
        raise ValueError(
            "timeout must not exceed "
            f"{MAX_AUTOMATION_TIMEOUT_SECONDS} seconds "
            f"({MAX_AUTOMATION_TIMEOUT_MINUTES} minutes)"
        )
    return timeout


def resolve_automation_timeout_seconds(timeout: int | None) -> int:
    """Return the effective run timeout in seconds.

    ``None`` preserves the configured service default (600 seconds by default), while
    explicit user values are capped by the public API limit as a defense in depth.
    """
    default_timeout = get_config().sandbox.max_run_duration
    effective_timeout = timeout if timeout is not None else default_timeout
    return min(effective_timeout, MAX_AUTOMATION_TIMEOUT_SECONDS)
