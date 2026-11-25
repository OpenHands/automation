"""SQL aggregation for factory-scoped outcome reads."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, Subquery, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import AutomationOutcomeValue
from openhands.automation.outcome_values import OutcomeAggregation


OutcomeBucketSize = Literal["day", "week", "month"]


@dataclass(frozen=True)
class Aggregate:
    value: float | int | None
    count: int


@dataclass(frozen=True)
class BucketAggregate(Aggregate):
    bucket: str


def effective_facts(conditions: Sequence[ColumnElement[bool]]) -> Subquery:
    """The facts an aggregation reads, with ``at`` as the time they count at."""
    return (
        select(
            AutomationOutcomeValue.created_at.label("at"),
            AutomationOutcomeValue.value_type,
            AutomationOutcomeValue.numeric_value,
            AutomationOutcomeValue.boolean_value,
        )
        .where(*conditions)
        .subquery("facts")
    )


def _bucket(at: ColumnElement, group_by: str, dialect: str) -> ColumnElement:
    """The UTC day, Monday-start week or month of ``at`` as an ISO label."""
    if dialect == "sqlite":
        # Older SQLite rounds fractions to the millisecond, so 23:59:59.9996
        # would land in the next day. Cut at whole seconds first.
        whole_seconds = func.substr(at, 1, 19)
        return {
            "day": func.strftime("%Y-%m-%d", whole_seconds),
            "week": func.date(whole_seconds, "+1 day", "weekday 1", "-7 days"),
            "month": func.strftime("%Y-%m", whole_seconds),
        }[group_by]
    if dialect == "postgresql":
        utc = func.timezone("UTC", at)
        return {
            "day": func.to_char(utc, "YYYY-MM-DD"),
            "week": func.to_char(func.date_trunc("week", utc), "YYYY-MM-DD"),
            "month": func.to_char(utc, "YYYY-MM"),
        }[group_by]
    raise ValueError(f"Unsupported database dialect for outcome buckets: {dialect}")


def _operation_filter(
    facts: Subquery, operation: OutcomeAggregation
) -> list[ColumnElement[bool]]:
    if operation in {"sum", "avg", "min", "max"}:
        return [facts.c.numeric_value.is_not(None)]
    if operation in {"count_true", "count_false"}:
        return [facts.c.value_type == "boolean"]
    return []


def _operation_value(facts: Subquery, operation: OutcomeAggregation) -> ColumnElement:
    numeric, boolean = facts.c.numeric_value, facts.c.boolean_value
    if operation == "count":
        return func.count()
    if operation in {"count_true", "count_false"}:
        wanted = operation == "count_true"
        return func.coalesce(func.sum(case((boolean.is_(wanted), 1), else_=0)), 0)
    return {"sum": func.sum, "avg": func.avg, "min": func.min, "max": func.max}[
        operation
    ](numeric)


async def aggregate_total(
    session: AsyncSession,
    conditions: Sequence[ColumnElement[bool]],
    operation: OutcomeAggregation,
) -> Aggregate:
    """Aggregate the facts matching ``conditions``. No facts gives a count of 0."""
    facts = effective_facts(conditions)
    row = (
        await session.execute(
            select(_operation_value(facts, operation), func.count())
            .select_from(facts)
            .where(*_operation_filter(facts, operation))
        )
    ).one()
    return Aggregate(value=row[0], count=row[1])


async def aggregate_buckets(
    session: AsyncSession,
    conditions: Sequence[ColumnElement[bool]],
    operation: OutcomeAggregation,
    bucket_size: OutcomeBucketSize,
) -> list[BucketAggregate]:
    """Aggregate by UTC bucket, oldest first. Buckets with no facts are left out."""
    facts = effective_facts(conditions)
    bucket = _bucket(facts.c.at, bucket_size, session.get_bind().dialect.name)
    rows = await session.execute(
        select(bucket.label("bucket"), _operation_value(facts, operation), func.count())
        .select_from(facts)
        .where(*_operation_filter(facts, operation))
        .group_by(bucket)
        .order_by(bucket)
    )
    return [
        BucketAggregate(bucket=bucket, value=value, count=count)
        for bucket, value, count in rows
    ]
