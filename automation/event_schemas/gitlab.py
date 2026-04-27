"""
GitLab event schema registry.

Pydantic models for GitLab webhook events. Each payload class:
1. Validates the payload structure via Pydantic
2. Identifies itself via `event_key` property

Reference: https://docs.gitlab.com/ee/user/project/integrations/webhooks.html

Design Decision - extra="ignore":
    We use extra="ignore" on all nested models because GitLab's webhook payloads
    frequently change (adding new fields). Using extra="forbid" would break on
    every GitLab API update. The trade-off is:
    - Typos in field names won't error (mitigated by Pydantic's required fields)
    - New GitLab fields are silently ignored (acceptable - we only parse what we need)
    For critical fields we rely on, Pydantic's required field validation catches
    missing data.

Filtering is handled by the trigger_matcher module using JMESPath expressions
evaluated against the raw payload. Example filters:
    - project.path_with_namespace == 'org/repo'
    - glob(project.path_with_namespace, 'org/*')
    - icontains(object_attributes.description, '@openhands')
    - contains(labels[].title, 'bug')
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, computed_field

from automation.event_schemas import WebhookEvent


if TYPE_CHECKING:
    from automation.event_schemas import detection


# =============================================================================
# Shared Payload Models (reused across events)
# =============================================================================


class GitLabUser(BaseModel):
    """GitLab user (author, assignee, etc.)."""

    id: int
    username: str
    name: str = ""

    model_config = {"extra": "ignore"}


class GitLabProject(BaseModel):
    """GitLab project (repository)."""

    id: int
    name: str
    path_with_namespace: str
    default_branch: str = "main"
    visibility_level: int = 0  # 0=private, 10=internal, 20=public

    model_config = {"extra": "ignore"}


class GitLabLabel(BaseModel):
    """GitLab issue/MR label."""

    id: int
    title: str
    color: str = ""

    model_config = {"extra": "ignore"}


# =============================================================================
# Base Class for GitLab Events
# =============================================================================


class GitLabEvent(WebhookEvent):
    """
    Base class for all GitLab event payloads.

    Extends WebhookEvent with GitLab-specific fields common to all events.

    Filtering is handled by the trigger_matcher module using JMESPath
    expressions evaluated against the raw webhook payload.
    """

    _source: ClassVar[str] = "gitlab"
    _event_type: ClassVar[str]

    # All GitLab events have project and user
    project: GitLabProject
    user: GitLabUser

    @computed_field
    @property
    def event_key(self) -> str:
        """
        Unique identifier for this event instance.

        Format: "{event_type}.{action}" or "{event_type}" if no action.
        Examples: "merge_request.open", "push", "issue.close"
        """
        action = getattr(self, "action", None)
        if action:
            return f"{self._event_type}.{action}"
        return self._event_type


# =============================================================================
# Merge Request Events (GitLab equivalent of Pull Request)
# =============================================================================


class MergeRequestAttributes(BaseModel):
    """Merge request object_attributes."""

    id: int
    iid: int  # Internal ID (project-scoped)
    title: str
    state: str  # "opened", "closed", "merged"
    draft: bool = False
    source_branch: str
    target_branch: str
    action: str | None = None  # "open", "close", "merge", "update", etc.

    model_config = {"extra": "ignore"}


class MergeRequestPayload(GitLabEvent):
    """
    GitLab merge_request event.

    Triggered on MR activity: open, close, merge, update, etc.

    Event keys:
    - merge_request.open
    - merge_request.close
    - merge_request.merge
    - merge_request.update
    - merge_request.approved
    - merge_request.unapproved

    Common JMESPath filters:
    - object_attributes.target_branch == 'main'
    - glob(project.path_with_namespace, 'org/*')
    - contains(labels[].title, 'bug')
    """

    _event_type: ClassVar[str] = "merge_request"

    object_kind: str = "merge_request"
    object_attributes: MergeRequestAttributes
    labels: list[GitLabLabel] = []  # noqa: RUF012

    @computed_field
    @property
    def action(self) -> str | None:
        """Extract action from object_attributes."""
        return self.object_attributes.action


# =============================================================================
# Push Events
# =============================================================================


class PushCommit(BaseModel):
    """A commit in a push event."""

    id: str
    message: str
    author: dict[str, Any]  # {name, email}

    model_config = {"extra": "ignore"}


class PushPayload(GitLabEvent):
    """
    GitLab push event.

    Triggered when commits are pushed to a repository.

    Event key: "push" (no action field)

    Common JMESPath filters:
    - ref == 'refs/heads/main'
    - glob(ref, 'refs/heads/release/*')
    - starts_with(ref, 'refs/tags/')
    """

    _event_type: ClassVar[str] = "push"

    object_kind: str = "push"
    ref: str  # refs/heads/main
    before: str  # SHA before push
    after: str  # SHA after push
    commits: list[PushCommit] = []  # noqa: RUF012

    @property
    def branch(self) -> str:
        """Extract branch name from ref."""
        return self.ref.removeprefix("refs/heads/")

    @property
    def is_default_branch(self) -> bool:
        """Check if push is to default branch."""
        return self.branch == self.project.default_branch


# =============================================================================
# Tag Push Events
# =============================================================================


class TagPushPayload(GitLabEvent):
    """
    GitLab tag_push event.

    Triggered when a tag is created or deleted.

    Event key: "tag_push" (no action field)

    Common JMESPath filters:
    - glob(ref, 'refs/tags/v*')
    """

    _event_type: ClassVar[str] = "tag_push"

    object_kind: str = "tag_push"
    ref: str  # refs/tags/v1.0.0
    before: str  # SHA before (0000... for create)
    after: str  # SHA after (0000... for delete)

    @property
    def tag_name(self) -> str:
        """Extract tag name from ref."""
        return self.ref.removeprefix("refs/tags/")

    @property
    def is_create(self) -> bool:
        """Check if this is a tag creation (vs deletion)."""
        return self.before == "0" * 40


# =============================================================================
# Issue Events
# =============================================================================


class IssueAttributes(BaseModel):
    """Issue object_attributes."""

    id: int
    iid: int  # Internal ID (project-scoped)
    title: str
    state: str  # "opened", "closed"
    action: str | None = None  # "open", "close", "reopen", "update"

    model_config = {"extra": "ignore"}


class IssuePayload(GitLabEvent):
    """
    GitLab issue event.

    Triggered on issue activity: open, close, reopen, update.

    Event keys:
    - issue.open
    - issue.close
    - issue.reopen
    - issue.update
    """

    _event_type: ClassVar[str] = "issue"

    object_kind: str = "issue"
    object_attributes: IssueAttributes
    labels: list[GitLabLabel] = []  # noqa: RUF012

    @computed_field
    @property
    def action(self) -> str | None:
        """Extract action from object_attributes."""
        return self.object_attributes.action


# =============================================================================
# Note (Comment) Events
# =============================================================================


class NoteAttributes(BaseModel):
    """Note (comment) object_attributes."""

    id: int
    note: str  # Comment body
    noteable_type: str  # "Issue", "MergeRequest", "Snippet", "Commit"
    action: str | None = None  # Usually None for notes

    model_config = {"extra": "ignore"}


class NotePayload(GitLabEvent):
    """
    GitLab note event.

    Triggered when a comment is created on an issue, MR, snippet, or commit.
    This is GitLab's equivalent of GitHub's issue_comment and
    pull_request_review_comment events combined.

    Event key: "note" (no action - comments don't have actions in GitLab)

    Common JMESPath filters:
    - icontains(object_attributes.note, '@openhands')
    - object_attributes.noteable_type == 'MergeRequest'
    - glob(project.path_with_namespace, 'org/*')
    """

    _event_type: ClassVar[str] = "note"

    object_kind: str = "note"
    object_attributes: NoteAttributes
    # Context objects - only one is present depending on noteable_type
    merge_request: dict[str, Any] | None = None
    issue: dict[str, Any] | None = None
    commit: dict[str, Any] | None = None
    snippet: dict[str, Any] | None = None


# =============================================================================
# Pipeline Events
# =============================================================================


class PipelineAttributes(BaseModel):
    """Pipeline object_attributes."""

    id: int
    status: str  # "pending", "running", "success", "failed", "canceled"
    ref: str  # Branch or tag name
    source: str  # "push", "web", "trigger", "schedule", etc.

    model_config = {"extra": "ignore"}


class PipelinePayload(GitLabEvent):
    """
    GitLab pipeline event.

    Triggered on CI/CD pipeline status changes.

    Event keys (uses status as action):
    - pipeline.pending
    - pipeline.running
    - pipeline.success
    - pipeline.failed
    - pipeline.canceled

    Common JMESPath filters:
    - object_attributes.status == 'failed'
    - object_attributes.ref == 'main'
    - object_attributes.source == 'push'
    """

    _event_type: ClassVar[str] = "pipeline"

    object_kind: str = "pipeline"
    object_attributes: PipelineAttributes

    @computed_field
    @property
    def action(self) -> str:
        """Use pipeline status as action."""
        return self.object_attributes.status


# =============================================================================
# Event Registry
# =============================================================================


# Maps object_kind -> payload class
GITLAB_PAYLOAD_CLASSES: dict[str, type[GitLabEvent]] = {
    "merge_request": MergeRequestPayload,
    "push": PushPayload,
    "tag_push": TagPushPayload,
    "issue": IssuePayload,
    "note": NotePayload,
    "pipeline": PipelinePayload,
}


# =============================================================================
# Event Type Detection
# =============================================================================

# Detection rules: (event_type, jmespath_expression)
# GitLab webhooks always include `object_kind` field that identifies the event type
GITLAB_DETECTION_RULES: list[tuple[str, str]] = [
    ("merge_request", "object_kind == 'merge_request'"),
    ("push", "object_kind == 'push'"),
    ("tag_push", "object_kind == 'tag_push'"),
    ("issue", "object_kind == 'issue'"),
    ("note", "object_kind == 'note'"),
    ("pipeline", "object_kind == 'pipeline'"),
]

# Lazy-initialized detector (created on first use)
_detector: detection.EventTypeDetector | None = None


def _get_detector() -> detection.EventTypeDetector:
    """Get or create the GitLab event type detector."""
    from automation.event_schemas import detection

    global _detector
    if _detector is None:
        _detector = detection.EventTypeDetector(GITLAB_DETECTION_RULES, source="gitlab")
    return _detector


def detect_gitlab_event_type(payload: dict[str, Any]) -> str:
    """
    Detect GitLab event type from payload structure.

    GitLab payloads include an `object_kind` field that identifies the event type,
    making detection straightforward.

    Args:
        payload: The raw GitLab webhook payload

    Returns:
        The event type string (e.g., 'merge_request', 'push')

    Raises:
        ValueError: If event type cannot be determined from payload
    """
    return _get_detector().detect(payload)


# =============================================================================
# Parsing Functions
# =============================================================================


def parse_gitlab_event(event_type: str, payload: dict[str, Any]) -> GitLabEvent:
    """
    Parse a raw GitLab webhook payload into a typed event object.

    Args:
        event_type: The event type (from object_kind or detection)
        payload: The raw webhook payload

    Returns:
        A typed GitLabEvent subclass instance

    Raises:
        ValueError: If event_type is unknown
        ValidationError: If payload doesn't match expected structure
    """
    cls = GITLAB_PAYLOAD_CLASSES.get(event_type)
    if cls is None:
        raise ValueError(f"Unknown GitLab event type: {event_type}")
    return cls.model_validate(payload)


def parse_gitlab_event_auto(payload: dict[str, Any]) -> GitLabEvent:
    """
    Parse a raw GitLab webhook payload by auto-detecting the event type.

    This is the preferred method when the event type is not provided
    (e.g., when forwarded from another service without the header).

    Args:
        payload: The raw GitLab webhook payload

    Returns:
        A typed GitLabEvent subclass instance

    Raises:
        ValueError: If event type cannot be detected or is unsupported
        ValidationError: If payload doesn't match expected structure
    """
    event_type = detect_gitlab_event_type(payload)
    return parse_gitlab_event(event_type, payload)


def get_supported_event_types() -> list[str]:
    """Get list of all supported GitLab event types."""
    return list(GITLAB_PAYLOAD_CLASSES.keys())
