"""Structured status details for automation run lifecycle issues."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from openhands.automation.utils.callback_error import (
    CallbackError,
    format_callback_error,
    parse_callback_error_event,
)
from openhands.automation.utils.time import utcnow
from openhands.automation.utils.transient import TransientErrorInfo


class RunStatusPhase(StrEnum):
    DISPATCH = "dispatch"
    EXECUTION = "execution"
    CALLBACK = "callback"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"


class RunStatusDetailKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    SERVER_ERROR = "server_error"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    EXECUTION_ERROR = "execution_error"
    CONCURRENCY_LIMIT = "concurrency_limit"
    UNKNOWN = "unknown"


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def make_run_status_detail(
    *,
    phase: RunStatusPhase | str,
    kind: RunStatusDetailKind | str,
    detail: str,
    transient: bool,
    source: str | None = None,
    operation: str | None = None,
    code: str | None = None,
    status_code: int | None = None,
    fingerprint: str | None = None,
    previous: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable current/last run status detail."""
    now = utcnow().isoformat()
    phase_value = phase.value if isinstance(phase, RunStatusPhase) else phase
    kind_value = kind.value if isinstance(kind, RunStatusDetailKind) else kind
    fingerprint = fingerprint or ":".join(
        str(part)
        for part in (phase_value, source, operation, kind_value, status_code)
        if part is not None
    )

    same_issue = previous is not None and previous.get("fingerprint") == fingerprint
    if same_issue and previous is not None:
        first_seen_at = previous.get("first_seen_at", now)
        previous_count = previous.get("count")
    else:
        first_seen_at = now
        previous_count = None
    count = previous_count + 1 if isinstance(previous_count, int) else 1

    detail_payload: dict[str, Any] = {
        "phase": phase_value,
        "kind": kind_value,
        "detail": detail,
        "transient": transient,
        "fingerprint": fingerprint,
        "first_seen_at": first_seen_at,
        "last_seen_at": now,
        "count": count,
    }
    if source:
        detail_payload["source"] = source
    if operation:
        detail_payload["operation"] = operation
    if code:
        detail_payload["code"] = code
    if status_code is not None:
        detail_payload["status_code"] = status_code
    if extra:
        detail_payload.update(dict(extra))
    return detail_payload


def run_status_detail_from_transient_error(
    error_info: TransientErrorInfo,
    *,
    phase: RunStatusPhase | str,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert retryable infrastructure error info into run status metadata."""
    return make_run_status_detail(
        phase=phase,
        kind=error_info.kind.value,
        detail=error_info.detail,
        transient=True,
        source=error_info.source,
        operation=error_info.operation,
        status_code=error_info.status_code,
        fingerprint=error_info.fingerprint,
        previous=previous,
    )


def run_status_detail_from_callback_error(
    error: CallbackError,
    *,
    formatted_detail: str | None = None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert SDK completion callback errors into run status metadata."""
    typed_error = parse_callback_error_event(error)
    if typed_error is not None:
        classification = typed_error.classification
        extra: dict[str, Any] = {
            "formatted_detail": formatted_detail or format_callback_error(typed_error)
        }
        if classification is not None:
            extra["user_action"] = classification.user_action
            if classification.error_id:
                extra["error_id"] = classification.error_id

        return make_run_status_detail(
            phase=RunStatusPhase.CALLBACK,
            kind=(
                classification.kind.value
                if classification is not None
                else RunStatusDetailKind.EXECUTION_ERROR
            ),
            detail=typed_error.detail,
            transient=classification.retryable if classification is not None else False,
            source=typed_error.source or "sdk_callback",
            code=typed_error.code,
            previous=previous,
            extra=extra,
        )

    if isinstance(error, str):
        return make_run_status_detail(
            phase=RunStatusPhase.CALLBACK,
            kind=RunStatusDetailKind.EXECUTION_ERROR,
            detail=error,
            transient=False,
            source="sdk_callback",
            previous=previous,
        )
    assert isinstance(error, Mapping)

    classification = error.get("classification")
    classification_kind = None
    if isinstance(classification, Mapping):
        classification_kind = _string_value(classification.get("kind"))

    fallback_detail = formatted_detail or format_callback_error(error)
    return make_run_status_detail(
        phase=RunStatusPhase.CALLBACK,
        kind=classification_kind or RunStatusDetailKind.EXECUTION_ERROR,
        detail=_string_value(error.get("detail"))
        or _string_value(error.get("message"))
        or fallback_detail,
        transient=False,
        source=_string_value(error.get("source")) or "sdk_callback",
        code=_string_value(error.get("code")),
        previous=previous,
        extra={"formatted_detail": fallback_detail},
    )


def run_status_detail_from_exception(
    exc: BaseException,
    *,
    phase: RunStatusPhase | str,
    source: str,
    operation: str,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build status detail for an unstructured exception."""
    from openhands.automation.utils.transient import classify_httpx_transient_error

    error_info = classify_httpx_transient_error(
        exc,
        source=source,
        operation=operation,
    )
    if error_info is not None:
        return run_status_detail_from_transient_error(
            error_info,
            phase=phase,
            previous=previous,
        )

    return make_run_status_detail(
        phase=phase,
        kind=RunStatusDetailKind.UNKNOWN,
        detail=str(exc),
        transient=False,
        source=source,
        operation=operation,
        code=type(exc).__name__,
        previous=previous,
    )
