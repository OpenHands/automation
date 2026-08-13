"""Build and apply deterministic titles for preset automation conversations."""

from __future__ import annotations

import re
import sys
from typing import Any


MAX_CONVERSATION_TITLE_LENGTH = 200
_RUN_ID_LENGTH = 12
_TRIGGER_CONTEXT_LENGTH = 80


def _clean(value: Any) -> str:
    """Return a single-line representation suitable for conversation metadata."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _event_target(event: Any) -> str:
    """Extract a compact repository and issue/PR identifier when available."""
    if not isinstance(event, dict):
        return ""

    repository = event.get("repository")
    project = event.get("project")
    repo_name = ""
    if isinstance(repository, dict):
        repo_name = _clean(repository.get("full_name"))
    if not repo_name and isinstance(project, dict):
        repo_name = _clean(project.get("path_with_namespace"))

    item_number: Any = None
    for key in ("pull_request", "issue", "object_attributes"):
        item = event.get(key)
        if isinstance(item, dict):
            item_number = item.get("number", item.get("iid"))
            if item_number is not None:
                break

    if repo_name and isinstance(item_number, (int, str)):
        number = _clean(str(item_number))
        if number:
            return f"{repo_name}#{number}"
    return repo_name


def _trigger_context(event_context: dict[str, Any]) -> str:
    trigger_payload = event_context.get("trigger_payload")
    if not isinstance(trigger_payload, dict):
        trigger_payload = {}

    trigger_type = _clean(event_context.get("trigger"))
    if not trigger_type:
        trigger_type = _clean(trigger_payload.get("type")) or "run"

    if trigger_type == "event":
        source = _clean(trigger_payload.get("source"))
        target = _event_target(event_context.get("event"))
        return " ".join(part for part in (source or "event", target) if part)

    if trigger_type == "cron":
        schedule = _clean(trigger_payload.get("schedule"))
        return f"cron {schedule}" if schedule else "cron"

    return trigger_type


def build_conversation_title(event_context: Any) -> str:
    """Build a stable, distinguishable title without exposing the user prompt."""
    if not isinstance(event_context, dict):
        event_context = {}

    automation_name = _clean(event_context.get("automation_name")) or "Automation"
    trigger_context = _trigger_context(event_context)[:_TRIGGER_CONTEXT_LENGTH]
    run_id = _clean(event_context.get("run_id"))
    run_suffix = run_id[:_RUN_ID_LENGTH] if run_id else "unknown-run"
    suffix = f" | {trigger_context} | {run_suffix}"

    name_limit = max(1, MAX_CONVERSATION_TITLE_LENGTH - len(suffix))
    return f"{automation_name[:name_limit]}{suffix}"


def set_conversation_title(
    workspace: Any, conversation_id: Any, event_context: Any
) -> str | None:
    """Set conversation metadata without allowing a title failure to abort the run."""
    title = build_conversation_title(event_context)
    try:
        response = workspace.client.patch(
            f"/api/conversations/{conversation_id}", json={"title": title}
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"  WARNING: could not set conversation title: {exc}", file=sys.stderr)
        return None
    return title
