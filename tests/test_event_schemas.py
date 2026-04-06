"""Tests for event schema parsing and trigger matching."""

import pytest

from automation.event_schemas import (
    parse_event,
)
from automation.event_schemas.github import (
    IssueCommentPayload,
    IssuesPayload,
    PullRequestPayload,
    PushPayload,
    ReleasePayload,
)
from automation.schemas import EventTrigger
from automation.trigger_matcher import matches_trigger


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


class TestTriggerMatching:
    """Tests for trigger matching using JMESPath filters."""

    def _pr_payload(
        self, action: str = "opened", repo: str = "org/test-repo", branch: str = "main"
    ) -> dict:
        """Create a PR payload dict."""
        return {
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
                "labels": [{"name": "bug"}, {"name": "help-wanted"}],
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

    def _push_payload(self, repo: str = "org/test-repo", branch: str = "main") -> dict:
        """Create a push payload dict."""
        return {
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

    def _comment_payload(self, body: str, repo: str = "org/test-repo") -> dict:
        """Create an issue_comment payload dict."""
        return {
            "action": "created",
            "comment": {
                "id": 1,
                "body": body,
                "user": {"id": 1, "login": "testuser"},
            },
            "issue": {
                "number": 10,
                "title": "Test issue",
                "state": "open",
                "labels": [{"name": "bug"}],
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": repo,
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

    def test_exact_event_key_match(self):
        """Exact event key should match."""
        payload = self._pr_payload(action="opened")
        trigger = EventTrigger(source="github", on="pull_request.opened")

        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is True
        )
        assert (
            matches_trigger(trigger, "github", "pull_request.closed", payload) is False
        )

    def test_wildcard_event_key_match(self):
        """Wildcard event key should match."""
        payload = self._pr_payload(action="opened")
        trigger_match = EventTrigger(source="github", on="pull_request.*")
        trigger_nomatch = EventTrigger(source="github", on="issues.*")

        assert (
            matches_trigger(trigger_match, "github", "pull_request.opened", payload)
            is True
        )
        assert (
            matches_trigger(trigger_nomatch, "github", "pull_request.opened", payload)
            is False
        )

    def test_multiple_event_keys(self):
        """Should match if any event key matches."""
        payload = self._pr_payload(action="opened")
        trigger = EventTrigger(source="github", on=["push", "pull_request.opened"])

        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is True
        )
        assert matches_trigger(trigger, "github", "issues.opened", payload) is False

    def test_source_mismatch(self):
        """Different source should not match."""
        payload = self._pr_payload()
        trigger = EventTrigger(source="gitlab", on="pull_request.opened")

        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is False
        )

    def test_repository_filter(self):
        """Repository filter using JMESPath."""
        payload = self._pr_payload(repo="org/test-repo")

        # Exact match
        trigger = EventTrigger(
            source="github",
            on="pull_request.opened",
            filter="repository.full_name == 'org/test-repo'",
        )
        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is True
        )

        # No match
        trigger = EventTrigger(
            source="github",
            on="pull_request.opened",
            filter="repository.full_name == 'other/repo'",
        )
        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is False
        )

        # Wildcard match using glob()
        trigger = EventTrigger(
            source="github",
            on="pull_request.opened",
            filter="glob(repository.full_name, 'org/*')",
        )
        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is True
        )

    def test_branch_filter_push(self):
        """Branch filter for push events using JMESPath."""
        payload = self._push_payload(branch="main")

        # Exact match
        trigger = EventTrigger(
            source="github",
            on="push",
            filter="ref == 'refs/heads/main'",
        )
        assert matches_trigger(trigger, "github", "push", payload) is True

        # No match
        trigger = EventTrigger(
            source="github",
            on="push",
            filter="ref == 'refs/heads/develop'",
        )
        assert matches_trigger(trigger, "github", "push", payload) is False

        # Wildcard match
        payload_feature = self._push_payload(branch="feature/test")
        trigger = EventTrigger(
            source="github",
            on="push",
            filter="glob(ref, 'refs/heads/feature/*')",
        )
        assert matches_trigger(trigger, "github", "push", payload_feature) is True

    def test_branch_filter_pr(self):
        """Branch filter for PR base branch using JMESPath."""
        payload = self._pr_payload(branch="main")

        # Exact match
        trigger = EventTrigger(
            source="github",
            on="pull_request.opened",
            filter="pull_request.base.ref == 'main'",
        )
        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is True
        )

        # No match
        trigger = EventTrigger(
            source="github",
            on="pull_request.opened",
            filter="pull_request.base.ref == 'develop'",
        )
        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is False
        )

    def test_combined_filters(self):
        """Multiple filters using && (AND logic)."""
        payload = self._push_payload(repo="org/test-repo", branch="main")

        # Both match
        trigger = EventTrigger(
            source="github",
            on="push",
            filter=(
                "repository.full_name == 'org/test-repo' && "
                "ref == 'refs/heads/main'"
            ),
        )
        assert matches_trigger(trigger, "github", "push", payload) is True

        # Repository matches, branch doesn't
        trigger = EventTrigger(
            source="github",
            on="push",
            filter=(
                "repository.full_name == 'org/test-repo' && "
                "ref == 'refs/heads/develop'"
            ),
        )
        assert matches_trigger(trigger, "github", "push", payload) is False

    def test_no_filter(self):
        """No filter should match any payload."""
        payload = self._pr_payload()
        trigger = EventTrigger(source="github", on="pull_request.opened")

        assert (
            matches_trigger(trigger, "github", "pull_request.opened", payload) is True
        )


class TestIssueCommentFiltering:
    """Tests for issue_comment filtering using JMESPath."""

    def _comment_payload(self, body: str, repo: str = "org/test-repo") -> dict:
        """Create an issue_comment payload dict."""
        return {
            "action": "created",
            "comment": {
                "id": 1,
                "body": body,
                "user": {"id": 1, "login": "testuser"},
            },
            "issue": {
                "number": 10,
                "title": "Test issue",
                "state": "open",
                "labels": [{"name": "bug"}],
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": repo,
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

    def test_body_contains_match(self):
        """Comment containing @openhands-resolver should match."""
        payload = self._comment_payload("Please fix this issue @openhands-resolver")
        trigger = EventTrigger(
            source="github",
            on="issue_comment.created",
            filter="icontains(comment.body, '@openhands-resolver')",
        )

        assert (
            matches_trigger(trigger, "github", "issue_comment.created", payload) is True
        )

    def test_body_contains_no_match(self):
        """Comment without the keyword should not match."""
        payload = self._comment_payload("Regular comment without mention")
        trigger = EventTrigger(
            source="github",
            on="issue_comment.created",
            filter="icontains(comment.body, '@openhands-resolver')",
        )

        assert (
            matches_trigger(trigger, "github", "issue_comment.created", payload)
            is False
        )

    def test_body_contains_case_insensitive(self):
        """icontains should be case-insensitive."""
        payload = self._comment_payload("Please help @OpenHands-Resolver!")
        trigger = EventTrigger(
            source="github",
            on="issue_comment.created",
            filter="icontains(comment.body, '@openhands-resolver')",
        )

        assert (
            matches_trigger(trigger, "github", "issue_comment.created", payload) is True
        )

    def test_body_contains_with_repository_filter(self):
        """Combined body and repository filter."""
        payload = self._comment_payload(
            "@openhands-resolver please fix",
            repo="OpenHands/OpenHands",
        )

        # Both filters match
        trigger = EventTrigger(
            source="github",
            on="issue_comment.created",
            filter=(
                "glob(repository.full_name, 'OpenHands/*') && "
                "icontains(comment.body, '@openhands-resolver')"
            ),
        )
        assert (
            matches_trigger(trigger, "github", "issue_comment.created", payload) is True
        )

        # Body matches, repo doesn't
        trigger = EventTrigger(
            source="github",
            on="issue_comment.created",
            filter=(
                "repository.full_name == 'other/repo' && "
                "icontains(comment.body, '@openhands-resolver')"
            ),
        )
        assert (
            matches_trigger(trigger, "github", "issue_comment.created", payload)
            is False
        )


class TestCustomWebhookEvent:
    """Tests for custom (unknown source) webhook events."""

    def test_parse_custom_webhook_simple(self):
        """Custom webhooks should parse with simple JMESPath expression."""
        payload = {
            "type": "order.created",
            "data": {"order_id": "12345"},
        }

        event = parse_event("custom-source", payload, event_key_expr="type")

        assert event.source == "custom-source"
        assert event.event_key == "order.created"

    def test_parse_custom_webhook_nested(self):
        """Custom webhooks should parse with nested JMESPath expression."""
        payload = {
            "event": {"type": "order.created"},
            "data": {"order_id": "12345"},
        }

        event = parse_event("custom-source", payload, event_key_expr="event.type")

        assert event.source == "custom-source"
        assert event.event_key == "order.created"

    def test_parse_custom_webhook_fallback(self):
        """Custom webhooks should support JMESPath || fallback."""
        payload = {
            "event_name": "payment.completed",
            "data": {"amount": 100},
        }

        # Use || for fallback - try type first, then event_name
        event = parse_event("stripe", payload, event_key_expr="type || event_name")

        assert event.source == "stripe"
        assert event.event_key == "payment.completed"

    def test_custom_webhook_trigger_matching(self):
        """Custom webhook events should match triggers."""
        payload = {
            "event": {"type": "order.created"},
            "data": {"order_id": "12345"},
        }

        trigger = EventTrigger(source="custom-source", on="order.created")
        result = matches_trigger(trigger, "custom-source", "order.created", payload)
        assert result is True

        trigger = EventTrigger(source="custom-source", on="order.*")
        result = matches_trigger(trigger, "custom-source", "order.created", payload)
        assert result is True

        trigger = EventTrigger(source="custom-source", on="user.created")
        result = matches_trigger(trigger, "custom-source", "order.created", payload)
        assert result is False

    def test_custom_webhook_with_filter(self):
        """Custom webhooks should support JMESPath filters."""
        payload = {
            "type": "payment.completed",
            "data": {"amount": 150, "currency": "USD"},
        }

        # Filter on nested data
        trigger = EventTrigger(
            source="stripe",
            on="payment.completed",
            filter="data.amount > `100` && data.currency == 'USD'",
        )
        result = matches_trigger(trigger, "stripe", "payment.completed", payload)
        assert result is True

        # Filter doesn't match
        trigger = EventTrigger(
            source="stripe",
            on="payment.completed",
            filter="data.amount > `200`",
        )
        result = matches_trigger(trigger, "stripe", "payment.completed", payload)
        assert result is False


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
        """Custom webhook with missing event key should raise ValueError."""
        payload = {"data": "test"}

        with pytest.raises(ValueError) as exc_info:
            parse_event("custom-source", payload, event_key_expr="missing.path")

        assert "Could not extract event_key" in str(exc_info.value)
        assert "missing.path" in str(exc_info.value)
