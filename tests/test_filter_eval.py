"""Tests for JMESPath filter evaluation."""

import pytest

from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_filter,
    validate_filter,
)


class TestEvaluateFilter:
    """Tests for evaluate_filter function."""

    def test_simple_equality(self):
        """Test simple field equality."""
        payload = {"name": "test", "value": 42}
        assert evaluate_filter("name == 'test'", payload) is True
        assert evaluate_filter("value == `42`", payload) is True
        assert evaluate_filter("name == 'other'", payload) is False

    def test_contains_builtin(self):
        """Test built-in contains() function."""
        payload = {"labels": ["bug", "help-wanted", "good-first-issue"]}
        assert evaluate_filter("contains(labels, 'bug')", payload) is True
        assert evaluate_filter("contains(labels, 'feature')", payload) is False

    def test_contains_string(self):
        """Test contains() with string."""
        payload = {"body": "Please fix this @openhands-resolver"}
        assert evaluate_filter("contains(body, '@openhands')", payload) is True
        assert evaluate_filter("contains(body, 'other-bot')", payload) is False

    def test_nested_field_access(self):
        """Test accessing nested fields."""
        payload = {
            "repository": {"full_name": "OpenHands/OpenHands"},
            "comment": {"body": "Fix this", "user": {"login": "testuser"}},
        }
        assert (
            evaluate_filter("repository.full_name == 'OpenHands/OpenHands'", payload)
            is True
        )
        assert evaluate_filter("comment.user.login == 'testuser'", payload) is True

    def test_boolean_and(self):
        """Test && (AND) operator."""
        payload = {"a": True, "b": True, "c": False}
        assert evaluate_filter("a && b", payload) is True
        assert evaluate_filter("a && c", payload) is False

    def test_boolean_or(self):
        """Test || (OR) operator."""
        payload = {"a": True, "b": False, "c": False}
        assert evaluate_filter("a || b", payload) is True
        assert evaluate_filter("b || c", payload) is False

    def test_boolean_not(self):
        """Test ! (NOT) operator."""
        payload = {"draft": False}
        assert evaluate_filter("!draft", payload) is True
        payload = {"draft": True}
        assert evaluate_filter("!draft", payload) is False

    def test_array_projection(self):
        """Test array projection for extracting nested values."""
        payload = {
            "labels": [
                {"name": "bug", "color": "red"},
                {"name": "help-wanted", "color": "green"},
            ]
        }
        # Get all label names
        assert evaluate_filter("contains(labels[].name, 'bug')", payload) is True
        assert evaluate_filter("contains(labels[].name, 'feature')", payload) is False


class TestCustomFunctions:
    """Tests for custom JMESPath functions."""

    def test_glob_exact_match(self):
        """Test glob() with exact match."""
        payload = {"repo": "OpenHands/OpenHands"}
        assert evaluate_filter("glob(repo, 'OpenHands/OpenHands')", payload) is True
        assert evaluate_filter("glob(repo, 'other/repo')", payload) is False

    def test_glob_wildcard(self):
        """Test glob() with wildcard patterns."""
        payload = {"repo": "OpenHands/automation"}
        assert evaluate_filter("glob(repo, 'OpenHands/*')", payload) is True
        assert evaluate_filter("glob(repo, '*/automation')", payload) is True
        assert evaluate_filter("glob(repo, 'Other/*')", payload) is False

    def test_glob_question_mark(self):
        """Test glob() with ? wildcard (single char)."""
        payload = {"branch": "release-1"}
        assert evaluate_filter("glob(branch, 'release-?')", payload) is True
        assert evaluate_filter("glob(branch, 'release-??')", payload) is False

    def test_icontains_case_insensitive(self):
        """Test icontains() for case-insensitive matching."""
        payload = {"body": "Hey @OpenHands-Resolver please fix this!"}
        assert (
            evaluate_filter("icontains(body, '@openhands-resolver')", payload) is True
        )
        assert (
            evaluate_filter("icontains(body, '@OPENHANDS-RESOLVER')", payload) is True
        )
        assert evaluate_filter("icontains(body, 'other-bot')", payload) is False

    def test_regex_basic(self):
        """Test regex() with basic patterns."""
        payload = {"ref": "refs/tags/v1.2.3"}
        assert evaluate_filter(r"regex(ref, '^refs/tags/v')", payload) is True
        # JMESPath uses single quotes, so we need single backslash for regex
        assert (
            evaluate_filter("regex(ref, 'v[0-9]+\\.[0-9]+\\.[0-9]+')", payload) is True
        )
        assert evaluate_filter(r"regex(ref, '^refs/heads/')", payload) is False

    def test_regex_invalid_pattern(self):
        """Test regex() with invalid pattern returns False."""
        payload = {"text": "test"}
        # Invalid regex pattern should return False, not raise
        assert evaluate_filter(r"regex(text, '[invalid')", payload) is False

    def test_starts_with(self):
        """Test starts_with() function."""
        payload = {"ref": "refs/heads/main"}
        assert evaluate_filter("starts_with(ref, 'refs/heads/')", payload) is True
        assert evaluate_filter("starts_with(ref, 'refs/tags/')", payload) is False

    def test_ends_with(self):
        """Test ends_with() function."""
        payload = {"filename": "config.yaml"}
        assert evaluate_filter("ends_with(filename, '.yaml')", payload) is True
        assert evaluate_filter("ends_with(filename, '.json')", payload) is False

    def test_lower(self):
        """Test lower() function."""
        payload = {"login": "TestUser"}
        assert evaluate_filter("lower(login) == 'testuser'", payload) is True

    def test_upper(self):
        """Test upper() function."""
        payload = {"status": "pending"}
        assert evaluate_filter("upper(status) == 'PENDING'", payload) is True


class TestComplexExpressions:
    """Tests for complex filter expressions."""

    def test_openhands_resolver_mention(self):
        """Test matching @openhands-resolver mentions in comments."""
        payload = {
            "action": "created",
            "comment": {"body": "Please fix this @openhands-resolver"},
            "repository": {"full_name": "OpenHands/OpenHands"},
        }
        expr = (
            "glob(repository.full_name, 'OpenHands/*') && "
            "icontains(comment.body, '@openhands-resolver')"
        )
        assert evaluate_filter(expr, payload) is True

        # Different repo
        payload["repository"]["full_name"] = "Other/repo"
        assert evaluate_filter(expr, payload) is False

    def test_pr_with_label_on_main(self):
        """Test matching PRs with specific label to main branch."""
        payload = {
            "action": "opened",
            "pull_request": {
                "base": {"ref": "main"},
                "labels": [{"name": "bug"}, {"name": "priority-high"}],
            },
            "repository": {"full_name": "org/repo"},
        }
        expr = (
            "pull_request.base.ref == 'main' && "
            "contains(pull_request.labels[].name, 'bug')"
        )
        assert evaluate_filter(expr, payload) is True

        # Wrong branch
        payload["pull_request"]["base"]["ref"] = "develop"
        assert evaluate_filter(expr, payload) is False

    def test_push_to_release_branch(self):
        """Test matching pushes to release branches."""
        payload = {"ref": "refs/heads/release/1.0"}
        expr = "glob(ref, 'refs/heads/release/*')"
        assert evaluate_filter(expr, payload) is True

        payload["ref"] = "refs/heads/main"
        assert evaluate_filter(expr, payload) is False

    def test_exclude_bot_users(self):
        """Test excluding bot users from matching."""
        payload = {
            "comment": {"body": "@openhands-resolver fix this"},
            "sender": {"login": "human-user"},
        }
        expr = (
            "icontains(comment.body, '@openhands-resolver') && "
            "!ends_with(sender.login, '[bot]')"
        )
        assert evaluate_filter(expr, payload) is True

        # Bot user should not match
        payload["sender"]["login"] = "dependabot[bot]"
        assert evaluate_filter(expr, payload) is False

    def test_or_multiple_event_sources(self):
        """Test OR logic for multiple conditions."""
        payload = {"ref": "refs/heads/main"}
        expr = (
            "ref == 'refs/heads/main' || "
            "glob(ref, 'refs/heads/release/*') || "
            "starts_with(ref, 'refs/tags/')"
        )
        assert evaluate_filter(expr, payload) is True

        payload["ref"] = "refs/heads/release/2.0"
        assert evaluate_filter(expr, payload) is True

        payload["ref"] = "refs/tags/v1.0.0"
        assert evaluate_filter(expr, payload) is True

        payload["ref"] = "refs/heads/feature/test"
        assert evaluate_filter(expr, payload) is False


class TestValidateFilter:
    """Tests for validate_filter function."""

    def test_valid_expression(self):
        """Test validation of valid expressions."""
        is_valid, error = validate_filter("name == 'test'")
        assert is_valid is True
        assert error is None

        is_valid, error = validate_filter(
            "glob(repo, 'org/*') && icontains(body, '@bot')"
        )
        assert is_valid is True
        assert error is None

    def test_invalid_expression(self):
        """Test validation of invalid expressions."""
        is_valid, error = validate_filter("invalid(((")
        assert is_valid is False
        assert error is not None

    def test_empty_expression(self):
        """Test validation of empty expression."""
        is_valid, error = validate_filter("")
        assert is_valid is False
        assert error is not None


class TestErrorHandling:
    """Tests for error handling."""

    def test_missing_field_returns_falsy(self):
        """Missing fields should evaluate to falsy (null)."""
        payload = {"name": "test"}
        # Accessing missing field returns null, which is falsy
        assert evaluate_filter("missing_field", payload) is False

    def test_invalid_expression_raises(self):
        """Invalid expression should raise FilterEvaluationError."""
        with pytest.raises(FilterEvaluationError):
            evaluate_filter("invalid(((", {"test": True})

    def test_type_error_in_function(self):
        """Type error in function should raise FilterEvaluationError."""
        payload = {"value": 42}
        # glob expects strings, passing number should fail
        with pytest.raises(FilterEvaluationError):
            evaluate_filter("glob(value, '*')", payload)
