"""GitLab webhook event parsing.

The OpenHands server forwards events from the GitLab webhooks it installs on
groups and projects. Every GitLab payload names its kind in the top-level
``object_kind`` field. The event key adds the action GitLab reports for that
kind, so triggers read like GitHub's: ``merge_request.open``, ``issue.close``,
``pipeline.failed``.

Only ``object_kind`` is required. GitLab payload shapes differ by kind (push
events, for example, carry flat ``user_*`` fields instead of a ``user``
object), so the raw payload is kept as-is for filters.

Reference: https://docs.gitlab.com/user/project/integrations/webhook_events/
"""

from typing import Any, ClassVar

from pydantic import computed_field

from openhands.automation.event_schemas import WebhookEvent


# The kinds the OpenHands server's GitLab webhooks subscribe to, each with the
# payload path holding its action, or None when the kind has no action.
# GitLab sends job events with object_kind "build".
GITLAB_EVENT_ACTIONS: dict[str, str | None] = {
    "build": "build_status",
    "issue": "object_attributes.action",
    "merge_request": "object_attributes.action",
    "note": None,
    "pipeline": "object_attributes.status",
}

GITLAB_EVENT_TYPES: tuple[str, ...] = tuple(sorted(GITLAB_EVENT_ACTIONS))


def _lookup(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class GitLabEvent(WebhookEvent):
    """GitLab webhook event; the raw payload is kept for filtering."""

    _source: ClassVar[str] = "gitlab"

    object_kind: str
    action: str | None = None
    payload: dict[str, Any]

    @computed_field
    @property
    def event_key(self) -> str:
        """``{object_kind}.{action}``, or the kind alone when it has none."""
        if self.action:
            return f"{self.object_kind}.{self.action}"
        return self.object_kind


def parse_gitlab_event(payload: dict[str, Any]) -> GitLabEvent:
    """Parse a GitLab webhook payload.

    Raises:
        ValueError: The payload names no kind, or one these webhooks never
            send. The event router acknowledges it without matching.
    """
    object_kind = payload.get("object_kind")
    if not isinstance(object_kind, str) or object_kind not in GITLAB_EVENT_ACTIONS:
        raise ValueError(f"Unsupported gitlab event kind: {object_kind!r}")

    action_path = GITLAB_EVENT_ACTIONS[object_kind]
    action = _lookup(payload, action_path) if action_path else None
    return GitLabEvent(
        object_kind=object_kind,
        action=action if isinstance(action, str) and action else None,
        payload=payload,
    )


def get_supported_event_patterns() -> list[str]:
    """The ``on`` patterns a setup UI can offer for GitLab."""
    return [
        f"{kind}.*" if action_path else kind
        for kind, action_path in sorted(GITLAB_EVENT_ACTIONS.items())
    ]


def get_event_detection_rules() -> list[dict[str, str]]:
    """Rules the forwarder uses to skip kinds no automation asks for."""
    return [
        {"event_type": kind, "jmespath": f"object_kind == '{kind}'"}
        for kind in GITLAB_EVENT_TYPES
    ]
