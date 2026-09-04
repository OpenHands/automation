"""Tests for .github/scripts/check_pr_description.py (linked-issue gate)."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace


def _load_prod_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / ".github" / "scripts" / "check_pr_description.py"
    name = "check_pr_description"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_prod = _load_prod_module()
body_from_event = _prod.body_from_event
extract_linked_issue_numbers = _prod.extract_linked_issue_numbers
validate_linked_issue_ready = _prod.validate_linked_issue_ready


def test_body_from_event_reads_pull_request_body(tmp_path: Path):
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "pull_request": {"body": "Fixes #12"},
                "repository": {"full_name": "org/repo"},
            }
        )
    )

    body, repo = body_from_event(event_path)
    assert body == "Fixes #12"
    assert repo == "org/repo"


def test_main_validates_body_file(monkeypatch, tmp_path: Path):
    body_path = tmp_path / "body.md"
    body_path.write_text("Fixes #12\n")
    monkeypatch.setattr(
        _prod,
        "parse_args",
        lambda: SimpleNamespace(body_file=body_path, repo="org/repo", event_path=None),
    )
    monkeypatch.setattr(
        _prod,
        "fetch_issue_details",
        lambda repo, num, token: (["bug"], "2026-09-04T00:00:00Z"),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert _prod.main() == 1


def test_extract_linked_issue_numbers_keyword_and_bare_ref():
    body = (
        "Fixes #12\n"
        "Closes #12 again\n"
        "resolves #34\n"
        "## Issue Number\n"
        "Issue: #56, see also #12\n"
    )
    assert extract_linked_issue_numbers(body) == [12, 34, 56]


def test_extract_linked_issue_numbers_only_bare_ref_in_issue_section():
    body = "## Summary\n\nSome work.\n\n## Issue Number\n\n#7\n"
    assert extract_linked_issue_numbers(body) == [7]


def test_extract_linked_issue_numbers_issue_section_is_case_insensitive():
    body = "## issue NUMBER\n\n#7\n"
    assert extract_linked_issue_numbers(body) == [7]


def test_extract_linked_issue_numbers_no_bare_ref_outside_issue_section():
    # A bare `#42` in the Summary must not be treated as a linked issue.
    body = "## Summary\n\nSee #42 for background.\n"
    assert extract_linked_issue_numbers(body) == []


def test_extract_linked_issue_numbers_accepts_only_closing_keyword_forms():
    body = (
        "Fixes #1; fixed #2; closes #3; closed #4; resolves #5; resolved #6.\n"
        "I plan to fix #7, close #8, and resolve #9 later.\n"
        "We are fixing #10, closing #11, and resolving #12 now.\n"
    )
    assert extract_linked_issue_numbers(body) == [1, 2, 3, 4, 5, 6]


def test_extract_linked_issue_numbers_ignores_fenced_code_and_quotes():
    body = "> Fixes #1\n```markdown\nFixes #2\n```\n~~~\nCloses #3\n~~~\nResolves #4\n"
    assert extract_linked_issue_numbers(body) == [4]


def test_extract_linked_issue_numbers_ignores_unclosed_fence():
    body = "Resolves #1\n```markdown\nFixes #2\n"
    assert extract_linked_issue_numbers(body) == [1]


def test_extract_linked_issue_numbers_keyword_inside_word_is_ignored():
    body = "crucifixes #12, encloses #34, transfixes #56.\n"
    assert extract_linked_issue_numbers(body) == []


def test_validate_linked_issue_ready_passes_without_linked_issue():
    # The gate blocks unready *linked* issues; it does not force a link.
    assert (
        validate_linked_issue_ready("A small chore, no issue.\n", "org/repo", "token")
        == []
    )


def test_validate_linked_issue_ready_fails_without_repository(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("should not call the network without a repository")

    monkeypatch.setattr(_prod, "fetch_issue_details", _fail)
    errors = validate_linked_issue_ready("Fixes #12\n", None, "token")
    assert errors and "Repository identity" in errors[0]


def test_validate_linked_issue_ready_fails_without_token(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("should not call the network without a token")

    monkeypatch.setattr(_prod, "fetch_issue_details", _fail)
    errors = validate_linked_issue_ready("Fixes #12\n", "org/repo", None)
    assert errors and "GITHUB_TOKEN" in errors[0]


def test_validate_linked_issue_ready_passes_with_ready_label(monkeypatch):
    monkeypatch.setattr(
        _prod,
        "fetch_issue_details",
        lambda repo, num, token: (["ready-for-dev"], "2026-09-04T00:00:00Z"),
    )
    assert validate_linked_issue_ready("Fixes #12\n", "org/repo", "token") == []


def test_validate_linked_issue_ready_grandfathers_pre_rollout_issue(monkeypatch):
    monkeypatch.setattr(
        _prod,
        "fetch_issue_details",
        lambda repo, num, token: (["bug"], "2026-01-15T00:00:00Z"),
    )
    assert validate_linked_issue_ready("Fixes #12\n", "org/repo", "token") == []


def test_validate_linked_issue_ready_grandfathers_rollout_day_before_deployment(
    monkeypatch,
):
    # Opened on the rollout day (2026-09-03) before the workflow was deployed,
    # so it was never labeled. It must be exempt.
    monkeypatch.setattr(
        _prod,
        "fetch_issue_details",
        lambda repo, num, token: (["bug"], "2026-09-03T06:46:00Z"),
    )
    assert validate_linked_issue_ready("Fixes #12\n", "org/repo", "token") == []


def test_validate_linked_issue_ready_fails_for_new_not_ready_issue(monkeypatch):
    monkeypatch.setattr(
        _prod,
        "fetch_issue_details",
        lambda repo, num, token: (["bug"], "2026-09-04T00:00:00Z"),
    )
    errors = validate_linked_issue_ready("Fixes #12\n", "org/repo", "token")
    assert errors and "ready-for-dev" in errors[0]


def test_validate_linked_issue_ready_new_unready_not_masked_by_ready_sibling(
    monkeypatch,
):
    def _issues(repo, num, token):
        # #12 carries ready-for-dev; #34 is new and not ready.
        if num == 34:
            return ["bug"], "2026-09-04T00:00:00Z"
        return ["ready-for-dev"], "2026-09-04T00:00:00Z"

    monkeypatch.setattr(_prod, "fetch_issue_details", _issues)
    body = "Fixes #12 and Closes #34"
    errors = validate_linked_issue_ready(body, "org/repo", "token")
    assert "#34" in errors[0]
    assert "ready-for-dev" in errors[0]


def test_validate_linked_issue_ready_returns_error_when_all_issues_not_found(
    monkeypatch,
):
    def _missing(repo, num, token):
        raise urllib.error.HTTPError(
            "https://api.github.com", 404, "Not Found", HTTPMessage(), None
        )

    monkeypatch.setattr(_prod, "fetch_issue_details", _missing)
    errors = validate_linked_issue_ready("Fixes #12\n", "org/repo", "token")
    assert errors and "could not be found" in errors[0]


def test_validate_linked_issue_ready_missing_not_masked_by_valid_sibling(monkeypatch):
    def _issues(repo, num, token):
        if num == 34:
            raise urllib.error.HTTPError(
                "https://api.github.com", 404, "Not Found", HTTPMessage(), None
            )
        return ["ready-for-dev"], "2026-09-04T00:00:00Z"

    monkeypatch.setattr(_prod, "fetch_issue_details", _issues)
    errors = validate_linked_issue_ready(
        "Fixes #12 and Closes #34", "org/repo", "token"
    )
    assert len(errors) == 1
    assert "#34" in errors[0]
    assert "could not be found" in errors[0]
