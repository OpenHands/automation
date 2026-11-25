"""The SQL outcome aggregation agrees with a plain Python reference.

``reference_aggregate`` and ``reference_bucket`` are the in-memory reduction the
aggregate route used before it moved into SQL. The test seeds facts around the
bucket edges and checks every operation, grouping and filter against them on
both supported databases.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta
from statistics import mean

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from openhands.automation.models import AutomationOutcomeValue, Factory


@pytest.fixture
async def async_engine(dual_db_engine: AsyncEngine) -> AsyncEngine:
    return dual_db_engine


def reference_aggregate(rows, operation):
    if operation == "count":
        return len(rows)
    if operation == "count_true":
        return sum(1 for row in rows if row.boolean_value is True)
    if operation == "count_false":
        return sum(1 for row in rows if row.boolean_value is False)
    numbers = [row.numeric_value for row in rows if row.numeric_value is not None]
    if not numbers:
        return None
    return {"sum": sum, "avg": mean, "min": min, "max": max}[operation](numbers)


def reference_bucket(created_at, group_by):
    if group_by == "day":
        return created_at.date().isoformat()
    if group_by == "week":
        return (created_at.date() - timedelta(days=created_at.weekday())).isoformat()
    return f"{created_at.year:04d}-{created_at.month:02d}"


def utc(*args, **kwargs):
    return datetime(*args, tzinfo=UTC, **kwargs)


EDGE_TIMES = [
    utc(2026, 10, 31, 23, 59, 59, 999999),
    utc(2026, 11, 1, 0, 0, 0),
    utc(2026, 11, 1, 23, 59, 59),
    utc(2026, 11, 2, 0, 0, 0),
    utc(2026, 11, 29, 23, 59, 59, 999999),
    utc(2026, 11, 30, 0, 0, 0),
    utc(2026, 12, 31, 23, 59, 59),
    utc(2027, 1, 1, 0, 0, 0),
]


def seed(org_id, factory_id, automations):
    rng = random.Random(7)
    start = utc(2026, 10, 20)
    times = EDGE_TIMES + [
        start + timedelta(seconds=rng.randrange(80 * 24 * 3600)) for _ in range(120)
    ]
    rows = []
    for index, at in enumerate(times):
        kind = ("n", "b", "s")[index % 3]
        columns = {
            "n": {
                "value_type": "number",
                "numeric_value": round(rng.uniform(-50, 200), 3),
            },
            "b": {"value_type": "boolean", "boolean_value": rng.random() < 0.6},
            "s": {"value_type": "string", "text_value": f"v{index}"},
        }[kind]
        rows.append(
            AutomationOutcomeValue(
                org_id=org_id,
                factory_id=factory_id,
                automation_id=automations[index % 2],
                source=("agent", "user")[index % 5 == 0],
                key=kind,
                created_at=at,
                **columns,
            )
        )
    return rows


KEY_OPERATIONS = [
    ("n", ["sum", "avg", "min", "max", "count"]),
    ("b", ["count_true", "count_false", "count"]),
    ("s", ["count", "sum"]),
]


async def test_sql_aggregation_matches_the_python_reference(
    async_client, async_session, mock_authenticated_user
):
    org_id = mock_authenticated_user.org_id
    factory = Factory(org_id=org_id, name="Aggregation factory")
    async_session.add(factory)
    await async_session.commit()
    automations = [uuid.uuid4(), uuid.uuid4()]
    rows = seed(org_id, factory.id, automations)
    async_session.add_all(rows)
    await async_session.commit()

    window = {"since": utc(2026, 11, 1), "until": utc(2026, 11, 30)}
    filters = [
        {},
        window,
        {"automation_id": str(automations[0])},
        {"source": "user"},
    ]
    url = f"/api/automation/v1/factories/{factory.id}/outcomes/aggregate"
    checked = 0
    for key, operations in KEY_OPERATIONS:
        for operation in operations:
            for group_by in ("none", "day", "week", "month"):
                for extra in filters:
                    label = (key, operation, group_by, sorted(extra))
                    params = {
                        "key": key,
                        "operation": operation,
                        "group_by": group_by,
                        **{
                            name: value.isoformat() if name in window else value
                            for name, value in extra.items()
                        },
                    }
                    response = await async_client.get(url, params=params)
                    assert response.status_code == 200, (label, response.text)
                    body = response.json()

                    selected = [
                        row
                        for row in rows
                        if row.key == key
                        and ("since" not in extra or row.created_at >= extra["since"])
                        and ("until" not in extra or row.created_at < extra["until"])
                        and (
                            "automation_id" not in extra
                            or str(row.automation_id) == extra["automation_id"]
                        )
                        and ("source" not in extra or row.source == extra["source"])
                        and (
                            operation not in {"sum", "avg", "min", "max"}
                            or row.numeric_value is not None
                        )
                        and (
                            operation not in {"count_true", "count_false"}
                            or row.value_type == "boolean"
                        )
                    ]
                    if group_by == "none":
                        expected = [(None, selected)]
                        actual = [(None, body["value"], body["count"])]
                    else:
                        grouped = {}
                        for row in selected:
                            grouped.setdefault(
                                reference_bucket(row.created_at, group_by), []
                            ).append(row)
                        expected = sorted(grouped.items())
                        actual = [
                            (bucket["bucket"], bucket["value"], bucket["count"])
                            for bucket in body["buckets"]
                        ]
                    assert [item[0] for item in expected] == [
                        item[0] for item in actual
                    ], label
                    for (_, bucket_rows), (_, value, count) in zip(
                        expected, actual, strict=True
                    ):
                        reference = reference_aggregate(bucket_rows, operation)
                        assert count == len(bucket_rows), label
                        if reference is None:
                            assert value is None, label
                        else:
                            assert value == pytest.approx(reference), label
                    checked += 1
    assert checked == 4 * 4 * 10
