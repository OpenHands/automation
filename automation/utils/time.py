"""Time utility functions for the automation service."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    All datetimes in the automation service are stored as
    ``TIMESTAMP WITH TIME ZONE`` (PostgreSQL *timestamptz*), which
    normalises every value to UTC on write.  Returning an aware
    ``datetime`` here guarantees the database column type is honoured
    end-to-end.
    """
    return datetime.now(UTC)
