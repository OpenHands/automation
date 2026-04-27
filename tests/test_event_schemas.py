"""Tests for event schema parsing and trigger matching."""

import pytest

from automation.event_schemas import (
    parse_event,
)
from automation.event_schemas.detection import EventTypeDetector
from automation.event_schemas.github import (
    GITHUB_DETECTION_RULES,
    IssueCommentPayload,
    IssuesPayload,
    PullRequestPayload,
    PullRequestReviewPayload,
    PushPayload,
    ReleasePayload,
    detect_github_event_type,
    parse_github_event_auto,
)
from automation.event_schemas.gitlab import (
    GITLAB_DETECTION_RULES,
    IssuePayload as GitLabIssuePayload,
    MergeRequestPayload,
    NotePayload,
    PipelinePayload,
    PushPayload as GitLabPushPayload,
    TagPushPayload,
    detect_gitlab_event_type,
    parse_gitlab_event_auto,
)
from automation.schemas import EventTrigger
from automation.trigger_matcher import matches_trigger


class TestGitHubEventParsing:
    """Tests for GitHub event parsing with auto-detection."""

    def test_parse_pull_request_opened(self):
        """Parse pull_request.opened event via auto-detection."""
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

        event = parse_event("github", payload)

        assert isinstance(event, PullRequestPayload)
        assert event.event_key == "pull_request.opened"
        assert event.source == "github"
        assert event.action == "opened"
        assert event.pull_request.number == 42

    def test_parse_push_event(self):
        """Parse push event via auto-detection."""
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

        event = parse_event("github", payload)

        assert isinstance(event, PushPayload)
        assert event.event_key == "push"
        assert event.ref == "refs/heads/main"

    def test_parse_issues_event(self):
        """Parse issues.opened event via auto-detection."""
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

        event = parse_event("github", payload)

        assert isinstance(event, IssuesPayload)
        assert event.event_key == "issues.opened"
        assert event.issue.number == 10

    def test_parse_issue_comment_event(self):
        """Parse issue_comment.created event via auto-detection."""
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

        event = parse_event("github", payload)

        assert isinstance(event, IssueCommentPayload)
        assert event.event_key == "issue_comment.created"
        assert event.comment.body == "Test comment"

    def test_parse_release_event(self):
        """Parse release.published event via auto-detection."""
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

        event = parse_event("github", payload)

        assert isinstance(event, ReleasePayload)
        assert event.event_key == "release.published"
        assert event.release.tag_name == "v1.0.0"

    def test_parse_unknown_payload_raises(self):
        """Unknown payload structure should raise ValueError."""
        payload = {
            "unknown_key": "test",
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

        with pytest.raises(ValueError, match="Cannot detect github event type"):
            parse_event("github", payload)


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
        filter_expr = (
            "repository.full_name == 'org/test-repo' && ref == 'refs/heads/main'"
        )
        trigger = EventTrigger(source="github", on="push", filter=filter_expr)
        assert matches_trigger(trigger, "github", "push", payload) is True

        # Repository matches, branch doesn't
        filter_expr = (
            "repository.full_name == 'org/test-repo' && ref == 'refs/heads/develop'"
        )
        trigger = EventTrigger(source="github", on="push", filter=filter_expr)
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
        # Payload has pull_request key so detection works, but missing required fields
        payload = {
            "action": "opened",
            "pull_request": {},  # Empty - missing required fields
            # Missing repository, sender
        }

        with pytest.raises(Exception):  # Pydantic ValidationError
            parse_event("github", payload)

    def test_empty_payload(self):
        """Empty payload should raise detection error."""
        with pytest.raises(ValueError, match="Cannot detect github event type"):
            parse_event("github", {})

    def test_custom_webhook_missing_event_type(self):
        """Custom webhook with missing event key should raise ValueError."""
        payload = {"data": "test"}

        with pytest.raises(ValueError) as exc_info:
            parse_event("custom-source", payload, event_key_expr="missing.path")

        assert "Could not extract event_key" in str(exc_info.value)
        assert "missing.path" in str(exc_info.value)


class TestEventTypeDetector:
    """Tests for the JMESPath-based EventTypeDetector."""

    def test_simple_key_detection(self):
        """Detect event type from single key presence."""
        # Use contains(keys(@), 'key') for reliable key existence checks
        detector = EventTypeDetector(
            [
                ("release", "contains(keys(@), 'release')"),
                ("push", "contains(keys(@), 'ref')"),
            ]
        )

        assert detector.detect({"release": {}}) == "release"
        assert detector.detect({"ref": "main"}) == "push"

    def test_compound_key_detection(self):
        """Detect event type from multiple key presence (AND)."""
        detector = EventTypeDetector(
            [
                (
                    "issue_comment",
                    "contains(keys(@), 'issue') && contains(keys(@), 'comment')",
                ),
                ("issues", "contains(keys(@), 'issue')"),
            ]
        )

        # Has both keys -> issue_comment
        assert detector.detect({"issue": {}, "comment": {}}) == "issue_comment"
        # Has only issue -> issues
        assert detector.detect({"issue": {}}) == "issues"

    def test_rule_order_specificity(self):
        """More specific rules should come first and win."""
        detector = EventTypeDetector(
            [
                (
                    "pull_request_review",
                    "contains(keys(@), 'pull_request') && contains(keys(@), 'review')",
                ),
                ("pull_request", "contains(keys(@), 'pull_request')"),
            ]
        )

        # Has both -> pull_request_review
        assert (
            detector.detect({"pull_request": {}, "review": {}}) == "pull_request_review"
        )
        # Has only pull_request -> pull_request
        assert detector.detect({"pull_request": {}}) == "pull_request"

    def test_no_match_raises(self):
        """ValueError raised when no rule matches."""
        detector = EventTypeDetector(
            [("push", "contains(keys(@), 'ref') && contains(keys(@), 'commits')")]
        )

        with pytest.raises(ValueError, match="Cannot detect"):
            detector.detect({"unknown_key": 123})

    def test_invalid_expression_raises_at_init(self):
        """Invalid JMESPath expression raises ValueError at init time."""
        with pytest.raises(ValueError, match="Invalid detection rule"):
            EventTypeDetector([("bad", "invalid(((")])

    def test_supported_types(self):
        """supported_types returns list of event types."""
        detector = EventTypeDetector(
            [
                ("type_a", "contains(keys(@), 'a')"),
                ("type_b", "contains(keys(@), 'b')"),
            ]
        )
        assert detector.supported_types == ["type_a", "type_b"]


class TestGitHubAutoDetection:
    """Tests for GitHub event type auto-detection."""

    def _base_payload(self) -> dict:
        """Base payload with required fields."""
        return {
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }

    def test_detect_pull_request(self):
        """Detect pull_request event."""
        payload = {
            **self._base_payload(),
            "action": "opened",
            "number": 42,
            "pull_request": {
                "number": 42,
                "title": "Test PR",
                "state": "open",
                "head": {"ref": "feature", "sha": "abc"},
                "base": {"ref": "main", "sha": "def"},
                "user": {"id": 1, "login": "testuser"},
            },
        }

        assert detect_github_event_type(payload) == "pull_request"

        event = parse_github_event_auto(payload)
        assert isinstance(event, PullRequestPayload)
        assert event.event_key == "pull_request.opened"

    def test_detect_pull_request_review(self):
        """Detect pull_request_review event (has both pull_request AND review)."""
        payload = {
            **self._base_payload(),
            "action": "submitted",
            "review": {"state": "approved"},
            "pull_request": {
                "number": 42,
                "title": "Test PR",
                "state": "open",
                "head": {"ref": "feature", "sha": "abc"},
                "base": {"ref": "main", "sha": "def"},
                "user": {"id": 1, "login": "testuser"},
            },
        }

        assert detect_github_event_type(payload) == "pull_request_review"

        event = parse_github_event_auto(payload)
        assert isinstance(event, PullRequestReviewPayload)
        assert event.event_key == "pull_request_review.submitted"

    def test_detect_issue_comment(self):
        """Detect issue_comment event (has both issue AND comment)."""
        payload = {
            **self._base_payload(),
            "action": "created",
            "issue": {
                "number": 10,
                "title": "Test issue",
                "state": "open",
                "user": {"id": 1, "login": "testuser"},
            },
            "comment": {
                "id": 1,
                "body": "Test comment",
                "user": {"id": 1, "login": "testuser"},
            },
        }

        assert detect_github_event_type(payload) == "issue_comment"

        event = parse_github_event_auto(payload)
        assert isinstance(event, IssueCommentPayload)
        assert event.event_key == "issue_comment.created"

    def test_detect_issues(self):
        """Detect issues event (has issue but NOT comment)."""
        payload = {
            **self._base_payload(),
            "action": "opened",
            "issue": {
                "number": 10,
                "title": "Test issue",
                "state": "open",
                "user": {"id": 1, "login": "testuser"},
            },
        }

        assert detect_github_event_type(payload) == "issues"

        event = parse_github_event_auto(payload)
        assert isinstance(event, IssuesPayload)
        assert event.event_key == "issues.opened"

    def test_detect_push(self):
        """Detect push event (has ref AND commits)."""
        payload = {
            **self._base_payload(),
            "ref": "refs/heads/main",
            "before": "abc123",
            "after": "def456",
            "commits": [],
        }

        assert detect_github_event_type(payload) == "push"

        event = parse_github_event_auto(payload)
        assert isinstance(event, PushPayload)
        assert event.event_key == "push"

    def test_detect_release(self):
        """Detect release event."""
        payload = {
            **self._base_payload(),
            "action": "published",
            "release": {
                "tag_name": "v1.0.0",
                "name": "Release 1.0.0",
            },
        }

        assert detect_github_event_type(payload) == "release"

        event = parse_github_event_auto(payload)
        assert isinstance(event, ReleasePayload)
        assert event.event_key == "release.published"

    def test_detect_unknown_raises(self):
        """Unknown payload structure raises ValueError."""
        payload = {
            **self._base_payload(),
            "unknown_event_type": {"data": "test"},
        }

        with pytest.raises(ValueError, match="Cannot detect github event type"):
            detect_github_event_type(payload)

    def test_parse_event_uses_auto_detection(self):
        """parse_event() uses auto-detection for GitHub payloads."""
        payload = {
            **self._base_payload(),
            "ref": "refs/heads/main",
            "before": "abc",
            "after": "def",
            "commits": [],
        }

        event = parse_event("github", payload)
        assert isinstance(event, PushPayload)
        assert event.event_key == "push"


class TestGitHubDetectionRules:
    """Tests for GITHUB_DETECTION_RULES configuration."""

    def test_rules_are_valid_jmespath(self):
        """All detection rules should be valid JMESPath expressions."""
        # This will raise if any expression is invalid
        detector = EventTypeDetector(GITHUB_DETECTION_RULES, source="github")
        assert len(detector.rules) == len(GITHUB_DETECTION_RULES)

    def test_rules_cover_all_payload_classes(self):
        """Detection rules should cover common event types."""
        detector = EventTypeDetector(GITHUB_DETECTION_RULES, source="github")
        supported = set(detector.supported_types)

        # Core event types should be detectable
        assert "pull_request" in supported
        assert "pull_request_review" in supported
        assert "issues" in supported
        assert "issue_comment" in supported
        assert "push" in supported
        assert "release" in supported


# =============================================================================
# GitLab Event Tests
# =============================================================================


class TestGitLabEventParsing:
    """Tests for GitLab event parsing with auto-detection."""

    def _base_payload(self) -> dict:
        """Base payload with required GitLab fields."""
        return {
            "project": {
                "id": 123,
                "name": "test-repo",
                "path_with_namespace": "org/test-repo",
                "default_branch": "main",
                "visibility_level": 0,
            },
            "user": {
                "id": 1,
                "username": "testuser",
                "name": "Test User",
            },
        }

    def test_parse_merge_request_open(self):
        """Parse merge_request.open event via auto-detection."""
        payload = {
            **self._base_payload(),
            "object_kind": "merge_request",
            "object_attributes": {
                "id": 1,
                "iid": 42,
                "title": "Test MR",
                "state": "opened",
                "draft": False,
                "source_branch": "feature/test",
                "target_branch": "main",
                "action": "open",
            },
            "labels": [],
        }

        event = parse_event("gitlab", payload)

        assert isinstance(event, MergeRequestPayload)
        assert event.event_key == "merge_request.open"
        assert event.source == "gitlab"
        assert event.object_attributes.iid == 42

    def test_parse_push_event(self):
        """Parse push event via auto-detection."""
        payload = {
            **self._base_payload(),
            "object_kind": "push",
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
        }

        event = parse_event("gitlab", payload)

        assert isinstance(event, GitLabPushPayload)
        assert event.event_key == "push"
        assert event.ref == "refs/heads/main"
        assert event.branch == "main"

    def test_parse_tag_push_event(self):
        """Parse tag_push event via auto-detection."""
        payload = {
            **self._base_payload(),
            "object_kind": "tag_push",
            "ref": "refs/tags/v1.0.0",
            "before": "0" * 40,
            "after": "def456",
        }

        event = parse_event("gitlab", payload)

        assert isinstance(event, TagPushPayload)
        assert event.event_key == "tag_push"
        assert event.tag_name == "v1.0.0"
        assert event.is_create is True

    def test_parse_issue_event(self):
        """Parse issue.open event via auto-detection."""
        payload = {
            **self._base_payload(),
            "object_kind": "issue",
            "object_attributes": {
                "id": 1,
                "iid": 10,
                "title": "Bug report",
                "state": "opened",
                "action": "open",
            },
            "labels": [],
        }

        event = parse_event("gitlab", payload)

        assert isinstance(event, GitLabIssuePayload)
        assert event.event_key == "issue.open"
        assert event.object_attributes.iid == 10

    def test_parse_note_event(self):
        """Parse note event via auto-detection."""
        payload = {
            **self._base_payload(),
            "object_kind": "note",
            "object_attributes": {
                "id": 1,
                "note": "Test comment @openhands help",
                "noteable_type": "MergeRequest",
            },
            "merge_request": {
                "id": 42,
                "iid": 5,
                "title": "Test MR",
            },
        }

        event = parse_event("gitlab", payload)

        assert isinstance(event, NotePayload)
        assert event.event_key == "note"
        assert event.object_attributes.note == "Test comment @openhands help"
        assert event.object_attributes.noteable_type == "MergeRequest"

    def test_parse_pipeline_event(self):
        """Parse pipeline.success event via auto-detection."""
        payload = {
            **self._base_payload(),
            "object_kind": "pipeline",
            "object_attributes": {
                "id": 1,
                "status": "success",
                "ref": "main",
                "source": "push",
            },
        }

        event = parse_event("gitlab", payload)

        assert isinstance(event, PipelinePayload)
        assert event.event_key == "pipeline.success"
        assert event.object_attributes.status == "success"

    def test_parse_unknown_payload_raises(self):
        """Unknown payload structure should raise ValueError."""
        payload = {
            **self._base_payload(),
            "object_kind": "unknown_event",
        }

        with pytest.raises(ValueError, match="Cannot detect gitlab event type"):
            parse_event("gitlab", payload)


class TestGitLabAutoDetection:
    """Tests for GitLab event type auto-detection."""

    def _base_payload(self) -> dict:
        """Base payload with required fields."""
        return {
            "project": {
                "id": 123,
                "name": "test-repo",
                "path_with_namespace": "org/test-repo",
                "default_branch": "main",
            },
            "user": {"id": 1, "username": "testuser", "name": "Test User"},
        }

    def test_detect_merge_request(self):
        """Detect merge_request event."""
        payload = {
            **self._base_payload(),
            "object_kind": "merge_request",
            "object_attributes": {
                "id": 1,
                "iid": 42,
                "title": "Test MR",
                "state": "opened",
                "source_branch": "feature",
                "target_branch": "main",
                "action": "open",
            },
        }

        assert detect_gitlab_event_type(payload) == "merge_request"

        event = parse_gitlab_event_auto(payload)
        assert isinstance(event, MergeRequestPayload)
        assert event.event_key == "merge_request.open"

    def test_detect_push(self):
        """Detect push event."""
        payload = {
            **self._base_payload(),
            "object_kind": "push",
            "ref": "refs/heads/main",
            "before": "abc123",
            "after": "def456",
            "commits": [],
        }

        assert detect_gitlab_event_type(payload) == "push"

        event = parse_gitlab_event_auto(payload)
        assert isinstance(event, GitLabPushPayload)
        assert event.event_key == "push"

    def test_detect_tag_push(self):
        """Detect tag_push event."""
        payload = {
            **self._base_payload(),
            "object_kind": "tag_push",
            "ref": "refs/tags/v1.0.0",
            "before": "0" * 40,
            "after": "def456",
        }

        assert detect_gitlab_event_type(payload) == "tag_push"

        event = parse_gitlab_event_auto(payload)
        assert isinstance(event, TagPushPayload)
        assert event.event_key == "tag_push"

    def test_detect_issue(self):
        """Detect issue event."""
        payload = {
            **self._base_payload(),
            "object_kind": "issue",
            "object_attributes": {
                "id": 1,
                "iid": 10,
                "title": "Test issue",
                "state": "opened",
                "action": "open",
            },
        }

        assert detect_gitlab_event_type(payload) == "issue"

        event = parse_gitlab_event_auto(payload)
        assert isinstance(event, GitLabIssuePayload)
        assert event.event_key == "issue.open"

    def test_detect_note(self):
        """Detect note event."""
        payload = {
            **self._base_payload(),
            "object_kind": "note",
            "object_attributes": {
                "id": 1,
                "note": "Test comment",
                "noteable_type": "Issue",
            },
        }

        assert detect_gitlab_event_type(payload) == "note"

        event = parse_gitlab_event_auto(payload)
        assert isinstance(event, NotePayload)
        assert event.event_key == "note"

    def test_detect_pipeline(self):
        """Detect pipeline event."""
        payload = {
            **self._base_payload(),
            "object_kind": "pipeline",
            "object_attributes": {
                "id": 1,
                "status": "failed",
                "ref": "main",
                "source": "push",
            },
        }

        assert detect_gitlab_event_type(payload) == "pipeline"

        event = parse_gitlab_event_auto(payload)
        assert isinstance(event, PipelinePayload)
        assert event.event_key == "pipeline.failed"

    def test_detect_unknown_raises(self):
        """Unknown object_kind raises ValueError."""
        payload = {
            **self._base_payload(),
            "object_kind": "unknown_event",
        }

        with pytest.raises(ValueError, match="Cannot detect gitlab event type"):
            detect_gitlab_event_type(payload)


class TestGitLabDetectionRules:
    """Tests for GITLAB_DETECTION_RULES configuration."""

    def test_rules_are_valid_jmespath(self):
        """All detection rules should be valid JMESPath expressions."""
        # This will raise if any expression is invalid
        detector = EventTypeDetector(GITLAB_DETECTION_RULES, source="gitlab")
        assert len(detector.rules) == len(GITLAB_DETECTION_RULES)

    def test_rules_cover_all_payload_classes(self):
        """Detection rules should cover common event types."""
        detector = EventTypeDetector(GITLAB_DETECTION_RULES, source="gitlab")
        supported = set(detector.supported_types)

        # Core event types should be detectable
        assert "merge_request" in supported
        assert "push" in supported
        assert "tag_push" in supported
        assert "issue" in supported
        assert "note" in supported
        assert "pipeline" in supported


class TestGitLabTriggerMatching:
    """Tests for GitLab event trigger matching using JMESPath filters."""

    def _mr_payload(
        self,
        action: str = "open",
        project: str = "org/test-repo",
        target_branch: str = "main",
    ) -> dict:
        """Create a merge request payload dict."""
        return {
            "object_kind": "merge_request",
            "object_attributes": {
                "id": 1,
                "iid": 42,
                "title": "Test MR",
                "state": "opened",
                "draft": False,
                "source_branch": "feature/test",
                "target_branch": target_branch,
                "action": action,
            },
            "project": {
                "id": 123,
                "name": project.split("/")[1],
                "path_with_namespace": project,
                "default_branch": "main",
            },
            "user": {"id": 1, "username": "testuser", "name": "Test User"},
            "labels": [{"id": 1, "title": "bug", "color": "#ff0000"}],
        }

    def _note_payload(self, note: str, project: str = "org/test-repo") -> dict:
        """Create a note (comment) payload dict."""
        return {
            "object_kind": "note",
            "object_attributes": {
                "id": 1,
                "note": note,
                "noteable_type": "MergeRequest",
            },
            "project": {
                "id": 123,
                "name": project.split("/")[1],
                "path_with_namespace": project,
                "default_branch": "main",
            },
            "user": {"id": 1, "username": "testuser", "name": "Test User"},
            "merge_request": {"id": 42, "iid": 5, "title": "Test MR"},
        }

    def test_match_mr_open_event(self):
        """Basic MR open event matching."""
        payload = self._mr_payload()
        trigger = EventTrigger(source="gitlab", on="merge_request.open")

        assert matches_trigger(trigger, "gitlab", "merge_request.open", payload) is True

    def test_match_mr_with_wildcard(self):
        """Wildcard matching for any MR action."""
        payload = self._mr_payload(action="merge")
        trigger = EventTrigger(source="gitlab", on="merge_request.*")

        assert (
            matches_trigger(trigger, "gitlab", "merge_request.merge", payload) is True
        )

    def test_match_mr_with_project_filter(self):
        """Filter MR by project path."""
        payload = self._mr_payload(project="org/test-repo")
        trigger = EventTrigger(
            source="gitlab",
            on="merge_request.open",
            filter="project.path_with_namespace == 'org/test-repo'",
        )

        assert matches_trigger(trigger, "gitlab", "merge_request.open", payload) is True

    def test_match_mr_with_glob_filter(self):
        """Filter MR by project glob pattern."""
        payload = self._mr_payload(project="org/test-repo")
        trigger = EventTrigger(
            source="gitlab",
            on="merge_request.open",
            filter="glob(project.path_with_namespace, 'org/*')",
        )

        assert matches_trigger(trigger, "gitlab", "merge_request.open", payload) is True

    def test_match_note_with_icontains(self):
        """Filter note by case-insensitive body match."""
        payload = self._note_payload("Please @OpenHands help with this")
        trigger = EventTrigger(
            source="gitlab",
            on="note",
            filter="icontains(object_attributes.note, '@openhands')",
        )

        assert matches_trigger(trigger, "gitlab", "note", payload) is True

    def test_no_match_different_source(self):
        """GitLab trigger should not match GitHub events."""
        payload = self._mr_payload()
        trigger = EventTrigger(source="github", on="pull_request.opened")

        assert (
            matches_trigger(trigger, "gitlab", "merge_request.open", payload) is False
        )

    def test_no_match_filter_mismatch(self):
        """Filter that doesn't match should not trigger."""
        payload = self._mr_payload(project="org/test-repo")
        trigger = EventTrigger(
            source="gitlab",
            on="merge_request.open",
            filter="project.path_with_namespace == 'different/repo'",
        )

        assert (
            matches_trigger(trigger, "gitlab", "merge_request.open", payload) is False
        )
