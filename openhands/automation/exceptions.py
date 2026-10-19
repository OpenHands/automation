"""Custom exceptions for the automation service.

These exceptions help distinguish between transient failures (which may
succeed on retry) and permanent failures (which should disable the automation).
"""


class PermanentDispatchError(Exception):
    """Base class for errors that indicate the automation is misconfigured.

    When raised, the automation should be disabled since retrying will not help.
    Examples: tarball URL doesn't exist, malformed configuration.

    These are distinct from transient errors like network timeouts or service
    unavailability, which may succeed on retry.
    """

    pass


class TarballNotFoundError(PermanentDispatchError):
    """The tarball could not be found - either the internal upload is missing
    or the external URL returns 404.

    This is a permanent error that warrants disabling the automation,
    since the configured tarball_path does not point to a valid resource.
    """

    pass


class ConcurrencyLimitReachedError(Exception):
    """The organization/workspace has reached its concurrent-sandbox limit.

    Unlike PermanentDispatchError this is a *transient*, org-level condition: a
    later run may succeed once a concurrent slot frees up. The run is marked
    SKIPPED (not FAILED) and the automation is left enabled.
    """

    pass


class DailySandboxLimitReachedError(Exception):
    """The user has reached their daily sandbox quota.

    Like the concurrency limit this is not a misconfiguration: retrying within
    the same UTC day cannot succeed (the quota resets at UTC midnight), so the
    run is marked SKIPPED (not FAILED) and the automation is left enabled.
    """

    pass


class SandboxProvisioningError(Exception):
    """A sandbox never became ready, so no automation code ran in it.

    The backend deletes the sandbox (best effort) before raising, so a retry
    with a fresh sandbox cannot duplicate any side effect.

    ``status`` is the last status the sandbox API reported, ``reason`` a
    user-safe summary of the runtime's ``status_detail`` (raw pod text can leak
    cluster internals), and ``timed_out`` whether the ready timeout expired
    while the sandbox was still starting.
    """

    def __init__(
        self,
        message: str,
        *,
        sandbox_id: str,
        status: str,
        reason: str | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.sandbox_id = sandbox_id
        self.status = status
        self.reason = reason
        self.timed_out = timed_out
        # Set by the dispatcher to the number of sandboxes tried.
        self.attempts = 1

    @property
    def code(self) -> str:
        if self.timed_out:
            return "sandbox_ready_timeout"
        return f"sandbox_{self.status.lower()}"
