"""Tests for automation failure classification and disablement thresholds."""

import pytest

from openhands.automation.exceptions import PermanentDispatchError
from openhands.automation.health import (
    classify_exception,
    failure_from_callback,
    next_consecutive_failure_count,
    should_disable,
)
from openhands.sdk.event.error_classification import FailureKind


def test_permanent_dispatch_errors_are_configuration_failures():
    failure = classify_exception(PermanentDispatchError("tarball not found"))

    assert failure.kind is FailureKind.CONFIG
    assert failure.counts_toward_disablement


def test_transient_errors_do_not_count_toward_disablement():
    failure = classify_exception(TimeoutError("upstream timed out"))

    assert failure.kind is FailureKind.TRANSIENT
    assert not failure.counts_toward_disablement
    assert next_consecutive_failure_count(2, failure) == 0


def test_agent_can_report_a_blocking_reason():
    failure = failure_from_callback(
        FailureKind.AGENT_ACTION,
        reason=None,
        blocking_reason="The requested integration is not connected",
    )

    assert failure.kind is FailureKind.AGENT_ACTION
    assert failure.counts_toward_disablement


def test_missing_callback_classification_is_unknown():
    failure = failure_from_callback(
        kind=None,
        reason="the SDK exited without a classification",
        blocking_reason=None,
    )

    assert failure.kind is FailureKind.UNKNOWN
    assert not failure.counts_toward_disablement


def test_disablement_happens_at_the_configured_threshold():
    failure = failure_from_callback(FailureKind.CONFIG, "invalid model", None)

    assert next_consecutive_failure_count(1, failure) == 2
    assert not should_disable(failure, consecutive_failures=2, threshold=3)
    assert should_disable(failure, consecutive_failures=3, threshold=3)


def test_threshold_must_be_positive():
    failure = failure_from_callback(FailureKind.CONFIG, "invalid model", None)

    with pytest.raises(ValueError, match="threshold must be positive"):
        should_disable(failure, consecutive_failures=1, threshold=0)


def test_sdk_typed_rate_limit_is_transient():
    from openhands.sdk.llm.exceptions import LLMRateLimitError

    failure = classify_exception(LLMRateLimitError())

    assert failure.kind is FailureKind.RATE_LIMIT
    assert not failure.counts_toward_disablement


def test_sdk_typed_bad_request_is_configuration_failure():
    from openhands.sdk.llm.exceptions import LLMBadRequestError

    failure = classify_exception(LLMBadRequestError())

    assert failure.kind is FailureKind.CONFIG
    assert failure.counts_toward_disablement


def test_transient_failure_breaks_a_configuration_failure_streak():
    config_failure = failure_from_callback(FailureKind.CONFIG, "bad model", None)
    transient_failure = failure_from_callback(FailureKind.TRANSIENT, "502", None)

    assert next_consecutive_failure_count(2, config_failure) == 3
    assert next_consecutive_failure_count(2, transient_failure) == 0


def test_legacy_callback_error_text_is_classified():
    rate_limit = failure_from_callback(None, "HTTP 429 from provider", None)
    provider_down = failure_from_callback(None, "HTTP 503 service unavailable", None)

    assert rate_limit.kind is FailureKind.RATE_LIMIT
    assert provider_down.kind is FailureKind.TRANSIENT


def test_value_errors_are_configuration_failures():
    failure = classify_exception(ValueError("invalid entrypoint"))

    assert failure.kind is FailureKind.CONFIG
    assert failure.counts_toward_disablement


def test_api_key_errors_are_authentication_failures():
    from openhands.automation.utils.api_key import APIKeyError

    failure = classify_exception(APIKeyError("HTTP 401: invalid API key"))

    assert failure.kind is FailureKind.AUTH
    assert failure.counts_toward_disablement


def test_bad_request_text_is_a_configuration_failure():
    failure = classify_exception(RuntimeError("HTTP 400: invalid model profile"))

    assert failure.kind is FailureKind.CONFIG
    assert failure.counts_toward_disablement


def test_legacy_model_configuration_text_is_a_configuration_failure():
    failure = failure_from_callback(None, "invalid base URL for provider", None)

    assert failure.kind is FailureKind.CONFIG
    assert failure.counts_toward_disablement
