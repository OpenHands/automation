"""Time utility functions for the automation service."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    All datetimes in the automation service are stored as
    ``TIMESTAMP WITH TIME ZONE`` (PostgreSQL *timestamptz*), which
    normalises every value to UTC on write.  Returning an aware
    ``datetime`` here guarantees the database column type is honoured
    end-to-end.
    """
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Attach UTC tzinfo to a naive datetime, leaving aware datetimes unchanged.

    SQLite ignores ``timezone=True`` on ``DateTime`` columns and always returns
    naive ``datetime`` objects. Without this, Pydantic serialises those values
    without a timezone suffix (e.g. ``"2026-03-23T09:00:00"``), which
    JavaScript's ``Date`` constructor interprets as *local* time instead of UTC.

    Applying this as an ``AfterValidator`` on every response-schema datetime
    field ensures the JSON output always includes a UTC offset regardless of the
    database backend.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# Annotated datetime type that guarantees UTC-aware serialisation.
# Use this instead of bare ``datetime`` in Pydantic response schemas so that
# naive datetimes returned by SQLite are transparently normalised to UTC.
UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
