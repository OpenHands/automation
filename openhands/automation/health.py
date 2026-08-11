"""Automation failure classification and disablement policy."""

from dataclasses import dataclass

from openhands.automation.exceptions import PermanentDispatchError
from openhands.sdk.event.error_classification import FailureKind, classify_error


_DISABLE_ELIGIBLE_KINDS = frozenset(
    {FailureKind.AUTH, FailureKind.CONFIG, FailureKind.QUOTA}
)


@dataclass(frozen=True, slots=True)
class AutomationFailure:
    """A terminal outcome relevant to automation health."""

    kind: FailureKind
    reason: str | None = None

    @property
    def counts_toward_disablement(self) -> bool:
        return self.kind in _DISABLE_ELIGIBLE_KINDS


def classify_exception(error: BaseException) -> AutomationFailure:
    """Classify an exception using the SDK's shared failure vocabulary."""
    if isinstance(error, (PermanentDispatchError, ValueError)):
        # Service-owned exceptions raised during dispatch indicate a
        # configuration problem (e.g. unsupported tarball path).
        kind = FailureKind.CONFIG
    else:
        kind = classify_error(type(error).__name__, str(error)).kind
    return AutomationFailure(kind=kind, reason=str(error) or type(error).__name__)
