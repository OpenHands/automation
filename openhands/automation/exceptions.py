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
