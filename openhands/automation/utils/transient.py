"""Shared transient infrastructure error classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx


class TransientErrorKind(StrEnum):
    """Normalized categories for retryable infrastructure errors."""

    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    SERVER_ERROR = "server_error"


@dataclass(frozen=True)
class TransientErrorInfo:
    """Structured description of a retryable infrastructure error."""

    source: str
    operation: str
    kind: TransientErrorKind
    status_code: int | None = None
    message: str | None = None

    @property
    def fingerprint(self) -> str:
        """Stable key for deduplicating repeated transient errors."""
        status = self.status_code if self.status_code is not None else ""
        return f"{self.source}:{self.operation}:{self.kind}:{status}"

    @property
    def detail(self) -> str:
        """Human-readable error detail."""
        if self.message:
            return self.message
        if self.status_code is not None:
            return f"{self.source} {self.operation} returned HTTP {self.status_code}"
        return f"{self.source} {self.operation} failed: {self.kind.value}"


def classify_httpx_transient_error(
    exc: BaseException,
    *,
    source: str,
    operation: str,
) -> TransientErrorInfo | None:
    """Return normalized transient info for retryable httpx errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429:
            kind = TransientErrorKind.RATE_LIMITED
        elif status_code == 408:
            kind = TransientErrorKind.TIMEOUT
        elif status_code >= 500:
            kind = TransientErrorKind.SERVER_ERROR
        else:
            return None
        return TransientErrorInfo(
            source=source,
            operation=operation,
            kind=kind,
            status_code=status_code,
            message=f"{source} {operation} returned HTTP {status_code}",
        )

    if isinstance(exc, httpx.TimeoutException):
        return TransientErrorInfo(
            source=source,
            operation=operation,
            kind=TransientErrorKind.TIMEOUT,
            message=f"{source} {operation} timed out: {exc}",
        )

    if isinstance(exc, httpx.TransportError):
        return TransientErrorInfo(
            source=source,
            operation=operation,
            kind=TransientErrorKind.TRANSPORT,
            message=f"{source} {operation} transport error: {exc}",
        )

    return None
