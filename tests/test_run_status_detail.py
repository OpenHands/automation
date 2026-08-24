"""Tests for structured automation run status details."""

import httpx

from openhands.automation.utils.run_status_detail import (
    RunStatusDetailKind,
    RunStatusPhase,
    blocking_factor_from_task_outcome,
    make_run_status_detail,
    run_status_detail_from_blocking_factor,
    run_status_detail_from_callback_error,
    run_status_detail_from_exception,
)


def test_make_run_status_detail_increments_matching_issue_count():
    previous = make_run_status_detail(
        phase=RunStatusPhase.VERIFICATION,
        kind=RunStatusDetailKind.RATE_LIMITED,
        detail="HTTP 429",
        transient=True,
        source="sandbox_api",
        operation="get_sandbox",
        status_code=429,
    )

    current = make_run_status_detail(
        phase=RunStatusPhase.VERIFICATION,
        kind=RunStatusDetailKind.RATE_LIMITED,
        detail="HTTP 429 again",
        transient=True,
        source="sandbox_api",
        operation="get_sandbox",
        status_code=429,
        previous=previous,
    )

    assert current["count"] == 2
    assert current["first_seen_at"] == previous["first_seen_at"]
    assert current["last_seen_at"]


def test_run_status_detail_from_callback_error_preserves_sdk_fields():
    detail = run_status_detail_from_callback_error(
        {
            "source": "environment",
            "code": "RuntimeError",
            "detail": "script crashed",
            "classification": {
                "kind": "auth",
                "retryable": False,
                "user_action": "settings",
            },
        },
        formatted_detail="RuntimeError: script crashed [kind=auth, source=environment]",
    )

    assert detail["phase"] == "callback"
    assert detail["kind"] == "auth"
    assert detail["source"] == "environment"
    assert detail["code"] == "RuntimeError"
    assert detail["detail"] == "script crashed"
    assert detail["transient"] is False
    assert detail["user_action"] == "settings"


def test_run_status_detail_from_blocking_factor_defaults_to_settings_action():
    detail = run_status_detail_from_blocking_factor(
        {"kind": "config", "reason": "Missing GitHub token", "source": "mcp"}
    )

    assert detail["phase"] == "callback"
    assert detail["kind"] == "config"
    assert detail["source"] == "mcp"
    assert detail["detail"] == "Missing GitHub token"
    assert detail["transient"] is False
    assert detail["user_action"] == "settings"
    assert detail["blocking_factor"] == {
        "kind": "config",
        "reason": "Missing GitHub token",
        "source": "mcp",
    }


def test_blocking_factor_from_failed_task_outcome_defaults_to_execution_error():
    blocking_factor = blocking_factor_from_task_outcome(
        {"success": False, "message": "Agent could not access Slack"}
    )

    assert blocking_factor is not None
    assert blocking_factor["kind"] == RunStatusDetailKind.EXECUTION_ERROR
    assert blocking_factor["detail"] == "Agent could not access Slack"


def test_blocking_factor_from_task_outcome_preserves_explicit_classification():
    blocking_factor = blocking_factor_from_task_outcome(
        {
            "success": False,
            "message": "Missing Slack token",
            "classification": {
                "kind": "config",
                "retryable": False,
                "user_action": "settings",
            },
        }
    )

    assert blocking_factor is not None
    detail = run_status_detail_from_blocking_factor(blocking_factor)

    assert detail["kind"] == "config"
    assert detail["transient"] is False
    assert detail["user_action"] == "settings"


def test_run_status_detail_from_exception_classifies_http_429():
    request = httpx.Request("POST", "https://example.test/api/v1/sandboxes")
    exc = httpx.HTTPStatusError(
        "rate limited",
        request=request,
        response=httpx.Response(429, request=request),
    )

    detail = run_status_detail_from_exception(
        exc,
        phase=RunStatusPhase.DISPATCH,
        source="sandbox_api",
        operation="create_sandbox",
    )

    assert detail["phase"] == "dispatch"
    assert detail["kind"] == "rate_limited"
    assert detail["transient"] is True
    assert detail["status_code"] == 429
    assert detail["fingerprint"] == "sandbox_api:create_sandbox:rate_limited:429"
