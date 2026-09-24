"""Tests for verification result compatibility and typed outcomes."""

from openhands.automation.utils.agent_server import (
    VerificationOutcome,
    VerificationResult,
)
from openhands.automation.utils.transient import (
    TransientErrorInfo,
    TransientErrorKind,
)


def test_legacy_success_fields_infer_completed_outcome():
    result = VerificationResult(verified=True, success=True, exit_code=0)

    assert result.outcome == VerificationOutcome.COMPLETED
    assert result.verified is True
    assert result.success is True
    assert result.transient is False


def test_legacy_failure_fields_infer_failed_outcome():
    result = VerificationResult(verified=True, success=False, exit_code=1)

    assert result.outcome == VerificationOutcome.FAILED
    assert result.verified is True
    assert result.success is False


def test_legacy_still_running_error_infers_still_running_outcome():
    result = VerificationResult(verified=False, error="No bash output found")

    assert result.outcome == VerificationOutcome.STILL_RUNNING
    assert result.verified is False
    assert result.success is None
    assert result.detail == "No bash output found"
    assert result.error == "No bash output found"


def test_legacy_transient_flag_infers_transient_outcome():
    result = VerificationResult(
        verified=False,
        error="sandbox API returned HTTP 429",
        transient=True,
    )

    assert result.outcome == VerificationOutcome.TRANSIENT_ERROR
    assert result.verified is False
    assert result.success is None
    assert result.transient is True
    assert result.detail == "sandbox API returned HTTP 429"


def test_outcome_constructor_preserves_legacy_transient_fields():
    error_info = TransientErrorInfo(
        source="sandbox_api",
        operation="get_sandbox",
        kind=TransientErrorKind.RATE_LIMITED,
        status_code=429,
    )

    result = VerificationResult(
        outcome=VerificationOutcome.TRANSIENT_ERROR,
        detail=error_info.detail,
        error_info=error_info,
    )

    assert result.outcome == VerificationOutcome.TRANSIENT_ERROR
    assert result.verified is False
    assert result.success is None
    assert result.transient is True
    assert result.error == error_info.detail
    assert result.error_info == error_info
    assert error_info.fingerprint == "sandbox_api:get_sandbox:rate_limited:429"
