"""Classify automation outcomes and decide when an automation is unhealthy."""

import re
from dataclasses import dataclass

from openhands.automation.exceptions import PermanentDispatchError
from openhands.sdk.event.error_classification import FailureKind, classify_error
from openhands.sdk.llm.exceptions import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMContextWindowExceedError,
    LLMMalformedConversationHistoryError,
    LLMRateLimitError,
    LLMServiceUnavailableError,
    LLMTimeoutError,
)


_DISABLE_ELIGIBLE_KINDS = frozenset(
    {FailureKind.AUTH, FailureKind.CONFIG, FailureKind.QUOTA}
)


@dataclass(frozen=True, slots=True)
class AutomationFailure:
    """Structured outcome metadata persisted on an automation run."""

    kind: FailureKind
    reason: str | None = None
    blocking_reason: str | None = None

    @property
    def counts_toward_disablement(self) -> bool:
        """Whether this outcome represents a repeatable user-side blocker."""
        return self.kind in _DISABLE_ELIGIBLE_KINDS or (
            self.kind is FailureKind.AGENT_ACTION and bool(self.blocking_reason)
        )


def classify_exception(error: BaseException) -> AutomationFailure:
    """Map a service exception to the SDK's shared failure vocabulary."""
    detail = str(error)
    if isinstance(error, PermanentDispatchError):
        kind = FailureKind.CONFIG
    elif isinstance(error, ValueError):
        kind = FailureKind.CONFIG
    elif isinstance(error, LLMAuthenticationError):
        kind = FailureKind.AUTH
    elif isinstance(error, LLMRateLimitError):
        kind = FailureKind.RATE_LIMIT
    elif isinstance(error, LLMBadRequestError):
        kind = FailureKind.CONFIG
    elif isinstance(error, (LLMTimeoutError, LLMServiceUnavailableError)):
        kind = FailureKind.TRANSIENT
    elif isinstance(
        error,
        (LLMContextWindowExceedError, LLMMalformedConversationHistoryError),
    ):
        kind = FailureKind.AGENT_ACTION
    else:
        kind = classify_error(type(error).__name__, detail).kind
        if kind is FailureKind.UNKNOWN:
            lowered = detail.casefold()
            if (
                "401" in lowered
                or "unauthorized" in lowered
                or "invalid api key" in lowered
                or ("api key" in lowered and "not found" in lowered)
            ):
                kind = FailureKind.AUTH
            elif re.search(r"\b(?:400|403|404)\b", lowered):
                kind = FailureKind.CONFIG
            elif any(
                token in lowered
                for token in (
                    "bad request",
                    "configuration error",
                    "invalid base url",
                    "invalid model",
                    "model not found",
                    "missing api key",
                    "no models loaded",
                    "provider not provided",
                )
            ):
                kind = FailureKind.CONFIG
            elif "429" in lowered or "rate limit" in lowered:
                kind = FailureKind.RATE_LIMIT
            elif (
                any(str(code) in lowered for code in (500, 502, 503, 504))
                or "timeout" in lowered
                or "timed out" in lowered
                or "connection" in lowered
                or "temporarily unavailable" in lowered
            ):
                kind = FailureKind.TRANSIENT

    reason = detail or type(error).__name__
    return AutomationFailure(kind=kind, reason=reason)


def failure_from_callback(
    kind: FailureKind | str | None,
    reason: str | None,
    blocking_reason: str | None,
) -> AutomationFailure:
    """Build a run outcome from the SDK callback's optional metadata."""
    if kind is None and blocking_reason:
        resolved_kind = FailureKind.AGENT_ACTION
    elif kind is None and reason:
        failure = classify_exception(RuntimeError(reason))
        return AutomationFailure(
            kind=failure.kind,
            reason=reason,
            blocking_reason=blocking_reason,
        )
    elif kind is None:
        resolved_kind = (
            FailureKind.AGENT_ACTION if blocking_reason else FailureKind.UNKNOWN
        )
    else:
        resolved_kind = FailureKind(kind)

    return AutomationFailure(
        kind=resolved_kind,
        reason=reason,
        blocking_reason=blocking_reason,
    )


def next_consecutive_failure_count(
    previous_count: int, failure: AutomationFailure
) -> int:
    """Return the next consecutive count for a terminal run outcome."""
    if failure.counts_toward_disablement:
        return previous_count + 1
    return 0


def should_disable(
    failure: AutomationFailure,
    *,
    consecutive_failures: int,
    threshold: int,
) -> bool:
    """Return whether an eligible failure reached the configured threshold."""
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    return failure.counts_toward_disablement and consecutive_failures >= threshold
