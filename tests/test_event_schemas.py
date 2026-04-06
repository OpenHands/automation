"""Tests for event schema parsing and matching."""

import pytest

from automation.event_schemas import (
    WebhookEvent,
    matches_filter_pattern,
    parse_event,
)
from automation.event_schemas.github import (
    IssueCommentPayload,
    IssuesPayload,
    PullRequestPayload,
    PushPayload,
    ReleasePayload,
)


class TestMatchesFilterPattern:
    """Tests for the matches_filter_pattern helper function."""

    def test_exact_match(self):
        """Exact match should return True."""
        assert matches_filter_pattern("main", ["main"]) is True
        assert matches_filter_pattern("feature/test", ["feature/test"]) is True

    def test_no_match(self):
        """Non-matching value should return False."""
        assert matches_filter_pattern("main", ["develop"]) is False
        assert matches_filter_pattern("feature/test", ["main"]) is False

    def test_wildcard_match(self):
        """Wildcard patterns should match correctly."""
        assert matches_filter_pattern("feature/test", ["feature/*"]) is True
        # fnmatch * matches everything including path separators
        assert matches_filter_pattern("feature/foo/bar", ["feature/*"]) is True
        assert matches_filter_pattern("release-1.0", ["release-*"]) is True
        # Wildcard at end
        assert matches_filter_pattern("feature-branch", ["feature-*"]) is True
        assert matches_filter_pattern("main", ["feature-*"]) is False

    def test_multiple_patterns(self):
        """Should match if any pattern matches."""
        assert matches_filter_pattern("main", ["main", "develop"]) is True
        assert matches_filter_pattern("develop", ["main", "develop"]) is True
        assert matches_filter_pattern("feature", ["main", "develop"]) is False

    def test_none_value(self):
        """None value should return False."""
        assert matches_filter_pattern(None, ["main"]) is False

    def test_empty_patterns(self):
        """Empty patterns list should return False."""
        assert matches_filter_pattern("main", []) is False


class TestGitHubEventParsing:
    """Tests for GitHub event parsing."""

    def test_parse_pull_request_opened(self):
        """Parse pull_request.opened event."""
        payload = {
            "action": "opened",
            "number": 42,
            "pull_request": {
                "id": 1,
                "number": 42,
                "title": "Test PR",
                "state": "open",
                "draft": False,
                "merged": False,
                "head": {"ref": "feature/test", "sha": "abc123"},
                "base": {"ref": "main", "sha": "def456"},
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        event = parse_event("github", payload, event_type="pull_request")

        assert isinstance(event, PullRequestPayload)
        assert event.event_key == "pull_request.opened"
        assert event.source == "github"
        assert event.action == "opened"
        assert event.pull_request.number == 42

    def test_parse_push_event(self):
        """Parse push event."""
        payload = {
            "ref": "refs/heads/main",
            "before": "abc123",
            "after": "def456",
            "commits": [
                {
                    "id": "def456",
                    "message": "Test commit",
                    "author": {"name": "Test", "email": "test@example.com"},
                }
            ],
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        event = parse_event("github", payload, event_type="push")

        assert isinstance(event, PushPayload)
        assert event.event_key == "push"
        assert event.ref == "refs/heads/main"

    def test_parse_issues_event(self):
        """Parse issues.opened event."""
        payload = {
            "action": "opened",
            "issue": {
                "id": 1,
                "number": 10,
                "title": "Bug report",
                "state": "open",
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        event = parse_event("github", payload, event_type="issues")

        assert isinstance(event, IssuesPayload)
        assert event.event_key == "issues.opened"
        assert event.issue.number == 10

    def test_parse_issue_comment_event(self):
        """Parse issue_comment.created event."""
        payload = {
            "action": "created",
            "comment": {
                "id": 1,
                "body": "Test comment",
                "user": {"id": 1, "login": "testuser"},
            },
            "issue": {
                "id": 1,
                "number": 10,
                "title": "Bug report",
                "state": "open",
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        event = parse_event("github", payload, event_type="issue_comment")

        assert isinstance(event, IssueCommentPayload)
        assert event.event_key == "issue_comment.created"
        assert event.comment.body == "Test comment"

    def test_parse_release_event(self):
        """Parse release.published event."""
        payload = {
            "action": "published",
            "release": {
                "tag_name": "v1.0.0",
                "name": "Version 1.0.0",
                "draft": False,
                "prerelease": False,
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        event = parse_event("github", payload, event_type="release")

        assert isinstance(event, ReleasePayload)
        assert event.event_key == "release.published"
        assert event.release.tag_name == "v1.0.0"

    def test_parse_unknown_event_type(self):
        """Unknown event type should raise ValueError."""
        payload = {
            "action": "test",
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        with pytest.raises(ValueError, match="Unknown GitHub event type"):
            parse_event("github", payload, event_type="unknown_event")


class TestGitHubEventMatching:
    """Tests for GitHub event matching against triggers."""

    def _create_pr_event(
        self, action: str = "opened", repo: str = "org/test-repo", branch: str = "main"
    ) -> WebhookEvent:
        """Helper to create a PR event."""
        payload = {
            "action": action,
            "number": 42,
            "pull_request": {
                "id": 1,
                "number": 42,
                "title": "Test PR",
                "state": "open",
                "draft": False,
                "merged": False,
                "head": {"ref": "feature/test", "sha": "abc123"},
                "base": {"ref": branch, "sha": "def456"},
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": repo.split("/")[1],
                "full_name": repo,
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }
        return parse_event("github", payload, event_type="pull_request")

    def _create_push_event(
        self, repo: str = "org/test-repo", branch: str = "main"
    ) -> WebhookEvent:
        """Helper to create a push event."""
        payload = {
            "ref": f"refs/heads/{branch}",
            "before": "abc123",
            "after": "def456",
            "commits": [],
            "repository": {
                "id": 123,
                "name": repo.split("/")[1],
                "full_name": repo,
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }
        return parse_event("github", payload, event_type="push")

    def test_exact_event_key_match(self):
        """Exact event key should match."""
        event = self._create_pr_event(action="opened")

        assert event.matches(["pull_request.opened"], {}) is True
        assert event.matches(["pull_request.closed"], {}) is False

    def test_wildcard_event_key_match(self):
        """Wildcard event key should match."""
        event = self._create_pr_event(action="opened")

        assert event.matches(["pull_request.*"], {}) is True
        assert event.matches(["issues.*"], {}) is False

    def test_multiple_event_keys(self):
        """Should match if any event key matches."""
        event = self._create_pr_event(action="opened")

        assert event.matches(["push", "pull_request.opened"], {}) is True
        assert event.matches(["push", "issues.opened"], {}) is False

    def test_repository_filter(self):
        """Repository filter should work."""
        event = self._create_pr_event(repo="org/test-repo")

        # Exact match
        assert (
            event.matches(["pull_request.opened"], {"repositories": ["org/test-repo"]})
            is True
        )

        # No match
        assert (
            event.matches(["pull_request.opened"], {"repositories": ["other/repo"]})
            is False
        )

        # Wildcard match
        assert (
            event.matches(["pull_request.opened"], {"repositories": ["org/*"]}) is True
        )

    def test_branch_filter_push(self):
        """Branch filter should work for push events."""
        event = self._create_push_event(branch="main")

        # Exact match
        assert event.matches(["push"], {"branches": ["main"]}) is True

        # No match
        assert event.matches(["push"], {"branches": ["develop"]}) is False

        # Wildcard match
        event_feature = self._create_push_event(branch="feature/test")
        assert event_feature.matches(["push"], {"branches": ["feature/*"]}) is True

    def test_branch_filter_pr(self):
        """Branch filter should work for PR base branch."""
        event = self._create_pr_event(branch="main")

        # Exact match
        assert event.matches(["pull_request.opened"], {"branches": ["main"]}) is True

        # No match
        assert (
            event.matches(["pull_request.opened"], {"branches": ["develop"]}) is False
        )

    def test_combined_filters(self):
        """Multiple filters should all apply (AND logic)."""
        event = self._create_push_event(repo="org/test-repo", branch="main")

        # Both match
        assert (
            event.matches(
                ["push"], {"repositories": ["org/test-repo"], "branches": ["main"]}
            )
            is True
        )

        # Repository matches, branch doesn't
        assert (
            event.matches(
                ["push"], {"repositories": ["org/test-repo"], "branches": ["develop"]}
            )
            is False
        )

        # Branch matches, repository doesn't
        assert (
            event.matches(
                ["push"], {"repositories": ["other/repo"], "branches": ["main"]}
            )
            is False
        )


class TestCustomWebhookEvent:
    """Tests for custom (unknown source) webhook events."""

    def test_parse_custom_webhook(self):
        """Custom webhooks should parse with event_type_paths."""
        payload = {
            "event": {"type": "order.created"},
            "data": {"order_id": "12345"},
        }

        event = parse_event("custom-source", payload, event_type_paths=["event.type"])

        assert event.source == "custom-source"
        assert event.event_key == "order.created"

    def test_custom_webhook_matches(self):
        """Custom webhook events should match on event key."""
        payload = {
            "event": {"type": "order.created"},
            "data": {"order_id": "12345"},
        }

        event = parse_event("custom-source", payload, event_type_paths=["event.type"])

        assert event.matches(["order.created"], {}) is True
        assert event.matches(["order.*"], {}) is True
        assert event.matches(["user.created"], {}) is False


class TestMalformedPayloads:
    """Tests for handling malformed payloads."""

    def test_missing_required_fields(self):
        """Missing required fields should raise validation error."""
        payload = {
            "action": "opened",
            # Missing pull_request, repository, sender
        }

        with pytest.raises(Exception):  # Pydantic ValidationError
            parse_event("github", payload, event_type="pull_request")

    def test_empty_payload(self):
        """Empty payload should raise error."""
        with pytest.raises(Exception):
            parse_event("github", {}, event_type="push")

    def test_custom_webhook_missing_event_type(self):
        """Custom webhook with missing event type path should raise ValueError."""
        payload = {"data": "test"}

        with pytest.raises(ValueError) as exc_info:
            parse_event("custom-source", payload, event_type_paths=["missing.path"])

        assert "Could not extract event_key" in str(exc_info.value)
        assert "missing.path" in str(exc_info.value)
