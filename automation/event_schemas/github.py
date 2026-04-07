"""
GitHub event schema registry.

Pydantic models for GitHub webhook events. Each payload class:
1. Validates the payload structure via Pydantic
2. Identifies itself via `event_key` property

Reference: https://docs.github.com/en/webhooks/webhook-events-and-payloads

Design Decision - extra="ignore":
    We use extra="ignore" on all nested models because GitHub's webhook payloads
    frequently change (adding new fields). Using extra="forbid" would break on
    every GitHub API update. The trade-off is:
    - Typos in field names won't error (mitigated by Pydantic's required fields)
    - New GitHub fields are silently ignored (acceptable - we only parse what we need)
    For critical fields we rely on, Pydantic's required field validation catches
    missing data. For optional fields with typos, integration tests with real
    GitHub payloads are the safety net.

Filtering is handled by the trigger_matcher module using JMESPath expressions
evaluated against the raw payload. Example filters:
    - repository.full_name == 'org/repo'
    - glob(repository.full_name, 'org/*')
    - icontains(comment.body, '@openhands-resolver')
    - contains(pull_request.labels[].name, 'bug')
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


class GitHubUser(BaseModel):
    """GitHub user (sender, author, etc.)."""

    id: int
    login: str
    type: str = "User"

    model_config = {"extra": "ignore"}


class GitHubRepository(BaseModel):
    """GitHub repository."""

    id: int
    name: str
    full_name: str
    private: bool
    default_branch: str = "main"

    model_config = {"extra": "ignore"}


class GitHubLabel(BaseModel):
    """GitHub issue/PR label."""

    name: str
    color: str = ""

    model_config = {"extra": "ignore"}


class GitHubRef(BaseModel):
    """Git reference (branch/tag info in PRs)."""

    ref: str
    sha: str

    model_config = {"extra": "ignore"}


# =============================================================================
# Base Class for GitHub Events
# =============================================================================


class GitHubEvent(WebhookEvent):
    """
    Base class for all GitHub event payloads.

    Extends WebhookEvent with GitHub-specific fields common to all events.

    Filtering is handled by the trigger_matcher module using JMESPath
    expressions evaluated against the raw webhook payload.
    """

    _source: ClassVar[str] = "github"
    _event_type: ClassVar[str]

    # All GitHub events have repository and sender
    repository: GitHubRepository
    sender: GitHubUser

    @computed_field
    @property
    def event_key(self) -> str:
        """
        Unique identifier for this event instance.

        Format: "{event_type}.{action}" or "{event_type}" if no action.
        Examples: "pull_request.opened", "push", "issues.closed"
        """
        action = getattr(self, "action", None)
        if action:
            return f"{self._event_type}.{action}"
        return self._event_type


# =============================================================================
# Pull Request Events
# =============================================================================


class PullRequest(BaseModel):
    """Pull request object."""

    number: int
    title: str
    state: str  # "open", "closed"
    draft: bool = False
    merged: bool = False
    base: GitHubRef
    head: GitHubRef
    labels: list[GitHubLabel] = []
    user: GitHubUser

    model_config = {"extra": "ignore"}


class PullRequestPayload(GitHubEvent):
    """
    GitHub pull_request event.

    Triggered on PR activity: opened, closed, synchronize, etc.

    Event keys:
    - pull_request.opened
    - pull_request.closed
    - pull_request.synchronize
    - pull_request.reopened
    - pull_request.edited
    - pull_request.labeled
    - pull_request.unlabeled
    - pull_request.ready_for_review
    - pull_request.converted_to_draft

    Common JMESPath filters:
    - pull_request.base.ref == 'main'
    - glob(repository.full_name, 'org/*')
    - contains(pull_request.labels[].name, 'bug')
    """

    _event_type: ClassVar[str] = "pull_request"

    action: str
    number: int
    pull_request: PullRequest


class PullRequestReviewPayload(GitHubEvent):
    """
    GitHub pull_request_review event.

    Triggered when a PR review is submitted, edited, or dismissed.

    Event keys:
    - pull_request_review.submitted
    - pull_request_review.edited
    - pull_request_review.dismissed
    """

    _event_type: ClassVar[str] = "pull_request_review"

    action: str
    review: dict[str, Any]  # {state: "approved"|"changes_requested"|"commented"}
    pull_request: PullRequest


# =============================================================================
# Issue Events
# =============================================================================


class Issue(BaseModel):
    """GitHub issue object."""

    number: int
    title: str
    state: str  # "open", "closed"
    labels: list[GitHubLabel] = []
    user: GitHubUser

    model_config = {"extra": "ignore"}


class IssuesPayload(GitHubEvent):
    """
    GitHub issues event.

    Triggered on issue activity: opened, closed, labeled, etc.

    Event keys:
    - issues.opened
    - issues.closed
    - issues.reopened
    - issues.edited
    - issues.labeled
    - issues.unlabeled
    - issues.assigned
    - issues.unassigned
    """

    _event_type: ClassVar[str] = "issues"

    action: str
    issue: Issue


class Comment(BaseModel):
    """GitHub comment object."""

    id: int
    body: str
    user: GitHubUser

    model_config = {"extra": "ignore"}


class IssueCommentPayload(GitHubEvent):
    """
    GitHub issue_comment event.

    Triggered when a comment is created/edited/deleted on an issue or PR.

    Event keys:
    - issue_comment.created
    - issue_comment.edited
    - issue_comment.deleted

    Common JMESPath filters:
    - icontains(comment.body, '@openhands-resolver')
    - glob(repository.full_name, 'org/*')
    - sender.login != 'bot'
    """

    _event_type: ClassVar[str] = "issue_comment"

    action: str
    issue: Issue
    comment: Comment


# =============================================================================
# Push Events
# =============================================================================


class PushCommit(BaseModel):
    """A commit in a push event."""

    id: str
    message: str
    author: dict[str, Any]  # {name, email}

    model_config = {"extra": "ignore"}


class PushPayload(GitHubEvent):
    """
    GitHub push event.

    Triggered when commits are pushed to a repository.

    Event key: "push" (no action field)

    Common JMESPath filters:
    - ref == 'refs/heads/main'
    - glob(ref, 'refs/heads/release/*')
    - starts_with(ref, 'refs/tags/')
    """

    _event_type: ClassVar[str] = "push"

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
        return self.branch == self.repository.default_branch


# =============================================================================
# Release Events
# =============================================================================


class Release(BaseModel):
    """GitHub release object."""

    tag_name: str
    name: str | None = None
    draft: bool = False
    prerelease: bool = False

    model_config = {"extra": "ignore"}


class ReleasePayload(GitHubEvent):
    """
    GitHub release event.

    Triggered on release activity: published, created, etc.

    Event keys:
    - release.published
    - release.created
    - release.released
    - release.prereleased
    - release.edited
    - release.deleted
    """

    _event_type: ClassVar[str] = "release"

    action: str
    release: Release


# =============================================================================
# Event Registry
# =============================================================================


# Maps event_type -> payload class
GITHUB_PAYLOAD_CLASSES: dict[str, type[GitHubEvent]] = {
    "pull_request": PullRequestPayload,
    "pull_request_review": PullRequestReviewPayload,
    "issues": IssuesPayload,
    "issue_comment": IssueCommentPayload,
    "push": PushPayload,
    "release": ReleasePayload,
}


# =============================================================================
# Event Type Detection
# =============================================================================

# Detection rules: (event_type, jmespath_expression)
# Order matters - more specific patterns must come first
#
# Note: We use contains(keys(@), 'key') to check for key existence because:
# - Direct key access returns the value, which is falsy for empty dicts/lists
# - `&&` in JMESPath returns the second operand if both truthy, not a boolean
# - contains(keys(@), 'key') always returns true/false based on key presence
GITHUB_DETECTION_RULES: list[tuple[str, str]] = [
    # PR review: has both pull_request AND review keys
    (
        "pull_request_review",
        "contains(keys(@), 'pull_request') && contains(keys(@), 'review')",
    ),
    # PR: has pull_request (but not review, checked above)
    ("pull_request", "contains(keys(@), 'pull_request')"),
    # Issue comment: has both issue AND comment keys
    ("issue_comment", "contains(keys(@), 'issue') && contains(keys(@), 'comment')"),
    # Issues: has issue (but not comment, checked above)
    ("issues", "contains(keys(@), 'issue')"),
    # Push: has ref AND commits keys
    ("push", "contains(keys(@), 'ref') && contains(keys(@), 'commits')"),
    # Release: has release key
    ("release", "contains(keys(@), 'release')"),
    # Workflow run: has workflow_run key
    ("workflow_run", "contains(keys(@), 'workflow_run')"),
    # Check run: has check_run key
    ("check_run", "contains(keys(@), 'check_run')"),
]

# Lazy-initialized detector (created on first use)
# Type annotation uses string literal to avoid forward reference issues
_detector: detection.EventTypeDetector | None = None


def _get_detector() -> detection.EventTypeDetector:
    """Get or create the GitHub event type detector."""
    from automation.event_schemas import detection

    global _detector
    if _detector is None:
        _detector = detection.EventTypeDetector(GITHUB_DETECTION_RULES, source="github")
    return _detector


def detect_github_event_type(payload: dict[str, Any]) -> str:
    """
    Detect GitHub event type from payload structure.

    Uses JMESPath expressions to identify the event type based on
    which keys are present in the payload.

    Args:
        payload: The raw GitHub webhook payload

    Returns:
        The event type string (e.g., 'pull_request', 'push')

    Raises:
        ValueError: If event type cannot be determined from payload
    """
    return _get_detector().detect(payload)


# =============================================================================
# Parsing Functions
# =============================================================================


def parse_github_event(event_type: str, raw_payload: dict[str, Any]) -> GitHubEvent:
    """
    Parse a raw GitHub webhook payload into a typed event object.

    Args:
        event_type: The event type (from X-GitHub-Event header or detection)
        raw_payload: The raw webhook payload

    Returns:
        A typed GitHubEvent subclass instance

    Raises:
        ValueError: If event_type is unknown
        ValidationError: If payload doesn't match expected structure
    """
    cls = GITHUB_PAYLOAD_CLASSES.get(event_type)
    if cls is None:
        raise ValueError(f"Unknown GitHub event type: {event_type}")
    return cls.model_validate(raw_payload)


def parse_github_event_auto(raw_payload: dict[str, Any]) -> GitHubEvent:
    """
    Parse a raw GitHub webhook payload by auto-detecting the event type.

    This is the preferred method when the event type is not provided
    (e.g., when forwarded from another service without the header).

    Args:
        raw_payload: The raw GitHub webhook payload

    Returns:
        A typed GitHubEvent subclass instance

    Raises:
        ValueError: If event type cannot be detected or is unsupported
        ValidationError: If payload doesn't match expected structure
    """
    event_type = detect_github_event_type(raw_payload)
    return parse_github_event(event_type, raw_payload)


def get_supported_event_types() -> list[str]:
    """Get list of all supported GitHub event types."""
    return list(GITHUB_PAYLOAD_CLASSES.keys())
