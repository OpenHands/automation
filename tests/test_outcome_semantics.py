"""Totals, increments, periods and dedupe keys through the outcome API."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from openhands.automation.models import Factory
from tests.test_outcome_api import make_automation, outcome_auth  # noqa: F401


SUBMIT = "/api/automation/v1/outcomes"
FACTORIES = "/api/automation/v1/factories"


@pytest.fixture
async def async_engine(dual_db_engine: AsyncEngine) -> AsyncEngine:
    return dual_db_engine


@pytest.fixture
async def world(async_session, mock_authenticated_user, outcome_auth):  # noqa: F811
    factory = Factory(
        org_id=mock_authenticated_user.org_id,
        name="Semantics factory",
        outcome_keys={
            "merged": {"type": "number", "semantics": "total", "grain": "week"},
            "deploys": {"type": "number", "semantics": "total", "grain": "day"},
            "lines": {"type": "number"},
        },
    )
    async_session.add(factory)
    await async_session.commit()
    first = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    second = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    return factory, first, second


async def submit(client, automation, **body):
    return await client.post(
        SUBMIT, params={"automation_id": str(automation.id)}, json=body
    )


async def aggregate(client, factory, key, operation="sum", **params):
    response = await client.get(
        f"{FACTORIES}/{factory.id}/outcomes/aggregate",
        params={"key": key, "operation": operation, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_later_total_replaces_an_earlier_one_for_the_same_period(
    async_client, world
):
    factory, automation, _ = world
    for period, value in [
        ("2025-W47", 15),
        ("2025-W48", 3),
        ("2025-W48", 7),
        ("2025-W48", 12),
    ]:
        response = await submit(
            async_client, automation, payload={"merged": value}, period=period
        )
        assert response.status_code == 201, response.text

    total = await aggregate(async_client, factory, "merged")
    assert (total["value"], total["count"]) == (27, 2)
    weekly = await aggregate(async_client, factory, "merged", group_by="week")
    assert [(b["bucket"], b["value"]) for b in weekly["buckets"]] == [
        ("2025-11-17", 15),
        ("2025-11-24", 12),
    ]
    listed = await async_client.get(
        f"{FACTORIES}/{factory.id}/outcomes/values", params={"key": "merged"}
    )
    assert listed.json()["total"] == 4
    assert {v["effective_at"] for v in listed.json()["values"]} == {
        "2025-11-17T00:00:00Z",
        "2025-11-24T00:00:00Z",
    }


async def test_a_period_counts_at_its_start_whatever_the_window_and_the_run_time(
    async_client, world
):
    factory, automation, _ = world
    await submit(async_client, automation, payload={"deploys": 4}, period="2025-11-23")
    await submit(async_client, automation, payload={"deploys": 6}, period="2025-11-24")

    window = {"since": "2025-11-24T00:00:00Z", "until": "2025-11-25T00:00:00Z"}
    assert (await aggregate(async_client, factory, "deploys", **window))["value"] == 6
    until_start = {"until": "2025-11-24T00:00:00Z"}
    assert (await aggregate(async_client, factory, "deploys", **until_start))[
        "value"
    ] == 4


async def test_totals_replace_only_within_the_same_automation_and_dimensions(
    async_client, world
):
    factory, first, second = world
    period = "2025-W48"
    for automation, dimensions, value in [
        (first, None, 5),
        (second, None, 7),
        (first, {"repo": "api"}, 3),
        (first, {"repo": "web"}, 4),
        (first, {"repo": "api"}, 6),
        (first, {"team": "x", "repo": "db"}, 1),
        (first, {"repo": "db", "team": "x"}, 2),
    ]:
        body = {"payload": {"merged": value}, "period": period}
        if dimensions:
            body["dimensions"] = dimensions
        response = await submit(async_client, automation, **body)
        assert response.status_code == 201, response.text

    # first: 5 + api 6 + web 4 + db 2; second: 7
    result = await aggregate(async_client, factory, "merged")
    assert (result["value"], result["count"]) == (24, 5)


async def test_a_total_must_carry_a_period_that_fits_its_grain(async_client, world):
    factory, automation, _ = world
    cases = [
        ({"payload": {"merged": 1}}, "Send period as YYYY-Www"),
        ({"payload": {"merged": 1}, "period": "2025-11-24"}, "Use YYYY-Www"),
        ({"payload": {"merged": 1}, "period": "2027-W53"}, "Use YYYY-Www"),
        ({"payload": {"merged": 1}, "period": "2999-W01"}, "future"),
        (
            {
                "payload": {"merged": 1},
                "period": "2025-W48",
                "effective_at": "2025-11-24T10:00:00Z",
            },
            "effective_at and dedupe_key",
        ),
        (
            {"payload": {"merged": 1}, "period": "2025-W48", "dedupe_key": "pr-1"},
            "effective_at and dedupe_key",
        ),
    ]
    for body, message in cases:
        response = await submit(async_client, automation, **body)
        assert response.status_code == 422, (body, response.text)
        assert message in response.text, (body, response.text)
    assert (await aggregate(async_client, factory, "merged"))["count"] == 0


async def test_an_increment_counts_when_it_happened_and_not_when_it_arrived(
    async_client, world
):
    factory, automation, _ = world
    for value, happened in [(2, "2025-11-23T23:30:00Z"), (3, "2025-11-24T00:30:00Z")]:
        response = await submit(
            async_client, automation, payload={"lines": value}, effective_at=happened
        )
        assert response.status_code == 201, response.text
    await submit(async_client, automation, payload={"lines": 10})

    daily = await aggregate(
        async_client,
        factory,
        "lines",
        group_by="day",
        until=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    assert [(b["bucket"], b["value"]) for b in daily["buckets"]] == [
        ("2025-11-23", 2),
        ("2025-11-24", 3),
    ]
    recent = await aggregate(
        async_client,
        factory,
        "lines",
        since=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    assert recent["value"] == 10


async def test_an_increment_refuses_a_period_and_a_future_time(async_client, world):
    _, automation, _ = world
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for body, message in [
        ({"payload": {"lines": 1}, "period": "2025-W48"}, "period applies only"),
        ({"payload": {"lines": 1}, "effective_at": future}, "future"),
    ]:
        response = await submit(async_client, automation, **body)
        assert response.status_code == 422, (body, response.text)
        assert message in response.text


async def test_a_dedupe_key_makes_an_increment_count_once(async_client, world):
    factory, first, second = world
    sent = await submit(async_client, first, payload={"lines": 5}, dedupe_key="pr-1")
    assert sent.status_code == 201 and sent.json() == {"stored": 1, "duplicates": 0}
    again = await submit(async_client, second, payload={"lines": 5}, dedupe_key="pr-1")
    assert again.status_code == 201 and again.json() == {"stored": 0, "duplicates": 1}
    other = await submit(async_client, first, payload={"lines": 7}, dedupe_key="pr-2")
    assert other.json() == {"stored": 1, "duplicates": 0}
    assert (await aggregate(async_client, factory, "lines"))["value"] == 12


async def test_a_dedupe_key_is_scoped_to_each_key_of_the_submission(
    async_client, world
):
    factory, automation, _ = world
    both = {"payload": {"lines": 1, "other": 2}, "dedupe_key": "event-1"}
    await async_client.put(
        f"{FACTORIES}/{factory.id}/outcome-keys/other", json={"type": "number"}
    )
    first = await submit(async_client, automation, **both)
    assert first.json() == {"stored": 2, "duplicates": 0}
    second = await submit(async_client, automation, **both)
    assert second.json() == {"stored": 0, "duplicates": 2}


async def test_the_registry_checks_semantics_and_protects_keys_in_use(
    async_client, world
):
    factory, automation, _ = world
    url = f"{FACTORIES}/{factory.id}/outcome-keys"
    for definition in [
        {"type": "number", "semantics": "total"},
        {"type": "string", "semantics": "total", "grain": "day"},
        {"type": "number", "grain": "day"},
        {"type": "number", "semantics": "total", "grain": "year"},
        {"type": "number", "semantics": "gauge"},
    ]:
        response = await async_client.put(f"{url}/fresh", json=definition)
        assert response.status_code == 422, (definition, response.text)

    created = await async_client.put(
        f"{url}/fresh", json={"type": "number", "semantics": "total", "grain": "month"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["outcome_keys"]["fresh"] == {
        "type": "number",
        "semantics": "total",
        "grain": "month",
    }
    unused = await async_client.put(
        f"{url}/fresh", json={"type": "number", "semantics": "total", "grain": "day"}
    )
    assert unused.status_code == 200, unused.text

    await submit(async_client, automation, payload={"merged": 1}, period="2025-W48")
    for definition in [
        {"type": "number", "semantics": "total", "grain": "day"},
        {"type": "number"},
    ]:
        refused = await async_client.put(f"{url}/merged", json=definition)
        assert refused.status_code == 409, refused.text
    same = await async_client.put(
        f"{url}/merged", json={"type": "number", "semantics": "total", "grain": "week"}
    )
    assert same.status_code == 200, same.text
