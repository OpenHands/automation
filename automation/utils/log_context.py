"""Structured logging context utilities."""

from typing import Any


def log_extra(
    run_id: str | None = None,
    sandbox_id: str | None = None,
    automation_id: str | None = None,
) -> dict[str, Any]:
    """Build extra dict for structured logging with contextual IDs.

    Args:
        run_id: The automation run ID.
        sandbox_id: The sandbox ID.
        automation_id: The automation definition ID.

    Returns:
        Dict with non-None values for use as logger extra parameter.
    """
    extra: dict[str, Any] = {}
    if run_id:
        extra["run_id"] = run_id
    if sandbox_id:
        extra["sandbox_id"] = sandbox_id
    if automation_id:
        extra["automation_id"] = automation_id
    return extra
