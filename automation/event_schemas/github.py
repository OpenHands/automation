"""
GitHub event schema registry.

Self-matching Pydantic models for GitHub webhook events. Each payload class:
1. Validates the payload structure via Pydantic
2. Identifies itself via `event_key` property
3. Can match itself against trigger conditions via `matches()` method

Reference: https://docs.github.com/en/webhooks/webhook-events-and-payloads
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, computed_field

from automation.event_schemas import WebhookEvent


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

    Extends WebhookEvent with GitHub-specific fields and filter support.

    Supported filters:
    - `repositories`: Repository names (e.g., ["org/repo"], ["org/*"])
    - `branches`: Branch names for push events (e.g., ["main", "develop"])

    Example:
        payload = PullRequestPayload.model_validate(raw)
        if payload.matches(on="pull_request.opened", filters={"repositories": ["org/repo"]}):
            # Trigger automation!
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

    def _matches_filters(self, filters: dict[str, list[str]]) -> bool:
        """
        Check GitHub-specific filters.

        Supported filters:
        - repositories: Match against repository.full_name
        - branches: Match against branch name (for push events)
        """
        # Repository filter
        if "repositories" in filters:
            if not self._filter_matches(self.repository.full_name, filters["repositories"]):
                return False

        # Branch filter (only applicable to events with branch info)
        if "branches" in filters:
            branch = self._get_branch()
            if branch is not None and not self._filter_matches(branch, filters["branches"]):
                return False

        return True

    def _get_branch(self) -> str | None:
        """Get branch name if applicable. Subclasses can override."""
        return None


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

    Supported filters:
    - repositories: Repository names
    - branches: Branch names (e.g., ["main", "develop"])
    """

    _event_type: ClassVar[str] = "push"

    ref: str  # refs/heads/main
    before: str  # SHA before push
    after: str  # SHA after push
    commits: list[PushCommit] = []

    @property
    def branch(self) -> str:
        """Extract branch name from ref."""
        return self.ref.removeprefix("refs/heads/")

    @property
    def is_default_branch(self) -> bool:
        """Check if push is to default branch."""
        return self.branch == self.repository.default_branch

    def _get_branch(self) -> str | None:
        """Return the branch name for branch filtering."""
        return self.branch


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


def parse_github_event(event_type: str, raw_payload: dict[str, Any]) -> GitHubEvent:
    """
    Parse a raw GitHub webhook payload into a typed event object.

    Args:
        event_type: The event type from X-GitHub-Event header
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


def get_supported_event_types() -> list[str]:
    """Get list of all supported GitHub event types."""
    return list(GITHUB_PAYLOAD_CLASSES.keys())



