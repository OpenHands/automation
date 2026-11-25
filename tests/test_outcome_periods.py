"""Which time an outcome fact counts at, and the checks that decide it."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from openhands.automation.outcome_periods import parse_period, resolve_effective_at
from openhands.automation.outcome_values import (
    OutcomeGrain,
    OutcomeSemantics,
    normalize_outcome_dimensions,
)


NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("grain", "period", "start"),
    [
        ("hour", "2026-11-24T14", datetime(2026, 11, 24, 14, tzinfo=UTC)),
        ("day", "2026-11-24", datetime(2026, 11, 24, tzinfo=UTC)),
        ("week", "2026-W48", datetime(2026, 11, 23, tzinfo=UTC)),
        ("week", "2026-W53", datetime(2026, 12, 28, tzinfo=UTC)),
        ("week", "2027-W01", datetime(2027, 1, 4, tzinfo=UTC)),
        ("month", "2026-11", datetime(2026, 11, 1, tzinfo=UTC)),
    ],
)
def test_a_period_starts_at_the_beginning_of_its_utc_hour_day_week_or_month(
    grain, period, start
):
    assert parse_period(period, grain) == start


@pytest.mark.parametrize(
    ("grain", "period", "form"),
    [
        ("day", "2026-W48", "YYYY-MM-DD"),
        ("week", "2026-11-24", "YYYY-Www"),
        ("month", "2026-11-24", "YYYY-MM"),
        ("hour", "2026-11-24", "YYYY-MM-DDTHH"),
        ("week", "2027-W53", "YYYY-Www"),
        ("week", "2026-W00", "YYYY-Www"),
        ("month", "2026-13", "YYYY-MM"),
        ("day", "2026-02-30", "YYYY-MM-DD"),
        ("hour", "2026-11-24T24", "YYYY-MM-DDTHH"),
        ("day", " 2026-11-24", "YYYY-MM-DD"),
    ],
)
def test_a_period_that_does_not_match_the_grain_names_the_expected_format(
    grain, period, form
):
    with pytest.raises(ValueError, match=f"Use {form}"):
        parse_period(period, grain)


def resolve(
    semantics: OutcomeSemantics = "increment",
    grain: OutcomeGrain | None = None,
    **fields,
):
    fields = {"period": None, "occurred_at": None, "dedupe_key": None, **fields}
    return resolve_effective_at("score", semantics, grain, now=NOW, **fields)


def test_a_total_counts_at_the_start_of_its_period():
    assert resolve("total", "week", period="2026-W47") == datetime(
        2026, 11, 16, tzinfo=UTC
    )


def test_a_period_that_has_begun_is_accepted_even_if_it_is_not_over():
    assert resolve("total", "month", period="2026-11") == datetime(
        2026, 11, 1, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({}, "Send period as YYYY-Www"),
        ({"period": "2026-W47", "occurred_at": NOW}, "occurred_at and dedupe_key"),
        ({"period": "2026-W47", "dedupe_key": "pr-1"}, "occurred_at and dedupe_key"),
        ({"period": "2026-W49"}, "future"),
        ({"period": "2026-11-24"}, "not a week period"),
    ],
)
def test_a_total_needs_exactly_a_period_of_its_grain_that_has_begun(fields, message):
    with pytest.raises(ValueError, match=message):
        resolve("total", "week", **fields)


def test_an_increment_counts_now_unless_it_says_when_it_happened():
    assert resolve() == NOW
    happened = datetime(2025, 3, 1, 9, 30, tzinfo=UTC)
    assert resolve(occurred_at=happened) == happened


def test_an_increment_time_is_converted_to_utc():
    offset = timezone(timedelta(hours=5))
    assert resolve(occurred_at=datetime(2026, 11, 24, 5, 30, tzinfo=offset)) == (
        datetime(2026, 11, 24, 0, 30, tzinfo=UTC)
    )
    assert resolve(occurred_at=datetime(2026, 11, 24, 5, 30)) == datetime(
        2026, 11, 24, 5, 30, tzinfo=UTC
    )


def test_an_increment_may_be_a_few_minutes_ahead_but_not_further():
    assert resolve(occurred_at=NOW + timedelta(minutes=5)) == NOW + timedelta(minutes=5)
    with pytest.raises(ValueError, match="future"):
        resolve(occurred_at=NOW + timedelta(minutes=6))


def test_an_increment_has_no_period():
    with pytest.raises(ValueError, match="period applies only to totals"):
        resolve(period="2026-11-24")


def test_dimensions_are_stored_with_sorted_keys():
    assert list(normalize_outcome_dimensions({"repo": "api", "env": "prod"})) == [
        "env",
        "repo",
    ]


@pytest.mark.parametrize(
    "dimensions",
    [
        {f"d{index}": "x" for index in range(9)},
        {"bad name": "x"},
        {"repo": ""},
        {"repo": "x" * 65},
    ],
)
def test_dimensions_that_are_too_many_badly_named_or_badly_sized_are_refused(
    dimensions,
):
    with pytest.raises(ValueError):
        normalize_outcome_dimensions(dimensions)
