"""When an outcome fact counts: its period or its time, checked per key."""

import re
from datetime import UTC, datetime, timedelta

from openhands.automation.outcome_values import OutcomeGrain, OutcomeSemantics
from openhands.automation.utils import utcnow


# A clock a few minutes ahead is normal. A time further ahead is a typo.
MAX_CLOCK_SKEW = timedelta(minutes=5)

# grain -> (pattern, format, example)
_PERIODS: dict[str, tuple[str, str, str]] = {
    "hour": (r"(\d{4})-(\d{2})-(\d{2})T(\d{2})", "YYYY-MM-DDTHH", "2026-11-24T14"),
    "day": (r"(\d{4})-(\d{2})-(\d{2})", "YYYY-MM-DD", "2026-11-24"),
    "week": (r"(\d{4})-W(\d{2})", "YYYY-Www, an ISO week", "2026-W48"),
    "month": (r"(\d{4})-(\d{2})", "YYYY-MM", "2026-11"),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_period(period: str, grain: OutcomeGrain) -> datetime:
    """The UTC start of ``period``. The text must match ``grain`` exactly."""
    pattern, form, example = _PERIODS[grain]
    match = re.fullmatch(pattern, period)
    try:
        if match is None:
            raise ValueError
        parts = [int(part) for part in match.groups()]
        if grain == "week":
            start = datetime.fromisocalendar(parts[0], parts[1], 1)
        elif grain == "month":
            start = datetime(parts[0], parts[1], 1)
        elif grain == "day":
            start = datetime(parts[0], parts[1], parts[2])
        else:
            start = datetime(parts[0], parts[1], parts[2], parts[3])
    except ValueError:
        raise ValueError(
            f"period {period!r} is not a {grain} period. "
            f"Use {form}, for example {example}"
        ) from None
    return start.replace(tzinfo=UTC)


def resolve_effective_at(
    key: str,
    semantics: OutcomeSemantics,
    grain: OutcomeGrain | None,
    *,
    period: str | None,
    effective_at: datetime | None,
    dedupe_key: str | None,
    now: datetime | None = None,
) -> datetime:
    """The time a fact counts at, after checking the fields fit the key.

    A ``total`` fact counts at the start of its period. An ``increment`` fact
    counts at the ``effective_at`` it was sent with, or now.
    """
    now = now or utcnow()
    if semantics == "total":
        if grain is None:
            raise ValueError(f"Outcome key {key!r} is a total with no grain")
        if period is None:
            _, form, example = _PERIODS[grain]
            raise ValueError(
                f"Outcome key {key!r} is a total. Send period as {form}, "
                f"for example {example}"
            )
        if effective_at is not None or dedupe_key is not None:
            raise ValueError(
                f"Outcome key {key!r} is a total. A later report for the same "
                "period replaces the earlier one, so effective_at and "
                "dedupe_key do not apply"
            )
        counts_at = parse_period(period, grain)
    else:
        if period is not None:
            raise ValueError(
                f"Outcome key {key!r} is an increment. period applies only to totals"
            )
        counts_at = _utc(effective_at) if effective_at is not None else now
    if counts_at > now + MAX_CLOCK_SKEW:
        raise ValueError(f"Outcome key {key!r} cannot count at a future time")
    return counts_at
