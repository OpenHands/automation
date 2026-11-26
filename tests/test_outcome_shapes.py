"""The table.v1 shape: key declarations and every write checked against them."""

import copy
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from openhands.automation.models import AutomationOutcomeValue, Factory
from openhands.automation.outcome_shapes import validate_table_value
from tests.test_outcome_api import make_automation, outcome_auth  # noqa: F401


SUBMIT = "/api/automation/v1/outcomes"
FACTORIES = "/api/automation/v1/factories"

COLUMNS = [
    {"name": "pr", "title": "PR", "type": "string"},
    {
        "name": "status",
        "type": "string",
        "constraints": {"required": True, "enum": ["open", "merged", "stale"]},
    },
    {"name": "lines", "type": "integer"},
    {"name": "score", "type": "number"},
    {"name": "draft", "type": "boolean"},
    {"name": "opened", "type": "datetime"},
]
TABLE_KEY = {"type": "json", "shape": "table.v1", "columns": COLUMNS}
ROW = ["#818", "open", 412, 0.5, False, "2026-11-24T10:00:00Z"]


def table(*rows, **extra):
    return {"rows": list(rows), **extra}


def with_cell(index, cell):
    row = list(ROW)
    row[index] = cell
    return table(row)


@pytest.fixture
async def async_engine(dual_db_engine: AsyncEngine) -> AsyncEngine:
    return dual_db_engine


@pytest.fixture
async def factory(async_client):
    created = await async_client.post(FACTORIES, json={"name": "Tables"})
    assert created.status_code == 201, created.text
    url = f"{FACTORIES}/{created.json()['id']}/outcome-keys/open_prs"
    put = await async_client.put(url, json=TABLE_KEY)
    assert put.status_code == 200, put.text
    return created.json()["id"]


@pytest.fixture
async def automation(async_session, mock_authenticated_user, factory, outcome_auth):  # noqa: F811
    row = await async_session.get(Factory, uuid.UUID(factory))
    return await make_automation(async_session, mock_authenticated_user, factory=row)


async def submit_table(client, automation, value):
    return await client.post(
        SUBMIT,
        params={"automation_id": str(automation.id)},
        json={"values": [{"key": "open_prs", "type": "json", "value": value}]},
    )


def key_with(**changes):
    definition = copy.deepcopy(TABLE_KEY)
    for name, value in changes.items():
        if value is None:
            definition.pop(name)
        else:
            definition[name] = value
    return definition


def column(**changes):
    return {"name": "c", "type": "string", **changes}


@pytest.mark.parametrize(
    "definition",
    [
        key_with(type="number"),
        key_with(type="string"),
        key_with(columns=None),
        key_with(shape=None),
        key_with(semantics="total", grain="day"),
        key_with(shape="table.v2"),
        key_with(columns=[]),
        key_with(columns=[column(name=f"c{i}") for i in range(21)]),
        key_with(columns=[column(), column()]),
        key_with(columns=[column(name="has space")]),
        key_with(columns=[column(type="duration")]),
        key_with(columns=[column(type="integer", constraints={"enum": ["1"]})]),
        key_with(columns=[column(constraints={"enum": []})]),
        key_with(columns=[column(constraints={"enum": ["a", "a"]})]),
        key_with(columns=[column(constraints={"enum": ["a" * 101]})]),
        key_with(columns=[column(constraints={"unique": True})]),
        key_with(columns=[column(format="uri")]),
    ],
    ids=lambda definition: str(
        {k: v for k, v in definition.items() if k != "columns"}
        | {"columns": len(definition.get("columns") or [])}
    ),
)
async def test_invalid_table_key_definitions_answer_422(
    async_client, factory, definition
):
    response = await async_client.put(
        f"{FACTORIES}/{factory}/outcome-keys/other", json=definition
    )
    assert response.status_code == 422, response.text


async def test_columns_without_a_shape_answer_422(async_client, factory):
    response = await async_client.put(
        f"{FACTORIES}/{factory}/outcome-keys/other",
        json={"type": "json", "columns": COLUMNS},
    )
    assert response.status_code == 422, response.text


async def test_the_key_route_returns_the_declared_contract(async_client, factory):
    keys = (await async_client.get(f"{FACTORIES}/{factory}/outcome-keys")).json()
    declared = keys["open_prs"]
    assert declared["shape"] == "table.v1"
    assert declared["columns"][0] == {
        "name": "pr",
        "title": "PR",
        "type": "string",
        "constraints": {"required": False, "enum": None},
    }
    assert declared["columns"][1]["constraints"] == {
        "required": True,
        "enum": ["open", "merged", "stale"],
    }


async def test_a_valid_table_is_stored_and_listed(
    async_client, async_session, factory, automation
):
    value = table(
        [
            {"v": "#818", "href": "https://github.com/OpenHands/automation/pull/818"},
            {"v": "merged", "f": "Merged"},
            None,
            {"v": 1.5, "f": "1.5 points"},
            True,
            {"v": None},
        ],
        ["#817", "open", 3, None, None, "2026-11-24T10:00:00+00:00"],
        total_rows=140,
    )
    response = await submit_table(async_client, automation, value)
    assert response.status_code == 201, response.text
    assert response.json() == {"stored": 1, "duplicates": 0}

    listed = await async_client.get(
        f"{FACTORIES}/{factory}/outcomes/values", params={"key": "open_prs"}
    )
    assert listed.json()["values"][0]["value"] == value


async def test_an_empty_table_is_valid(async_client, automation):
    response = await submit_table(async_client, automation, table(total_rows=0))
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("value", "where"),
    [
        (table(ROW[:-1]), "open_prs.rows[0] must be a list of 6 cells"),
        (table(ROW + [1]), "open_prs.rows[0] must be a list of 6 cells"),
        (table({"pr": "#1"}), "open_prs.rows[0] must be a list of 6 cells"),
        (with_cell(0, 7), "open_prs.rows[0][0] is not a string"),
        (with_cell(1, None), "open_prs.rows[0][1] is null"),
        (with_cell(1, "closed"), "open_prs.rows[0][1] must be one of"),
        (with_cell(2, 1.5), "open_prs.rows[0][2] is not an integer"),
        (with_cell(2, True), "open_prs.rows[0][2] is not an integer"),
        (with_cell(3, "0.5"), "'open_prs.rows[0][3]' is not a number"),
        (with_cell(3, True), "'open_prs.rows[0][3]' is not a number"),
        (with_cell(4, "yes"), "open_prs.rows[0][4] is not a boolean"),
        (with_cell(5, "yesterday"), "'open_prs.rows[0][5]' is not an ISO datetime"),
        (with_cell(0, {"f": "no value"}), "needs v"),
        (with_cell(0, {"v": "#1", "p": {}}), "needs v and only v, f, href"),
        (with_cell(0, {"v": 7}), "open_prs.rows[0][0].v is not a string"),
        (with_cell(0, {"v": "#1", "f": 7}), "open_prs.rows[0][0].f must be a string"),
        (with_cell(0, "x" * 2049), "exceeds 2048 characters"),
        (table(ROW, total_rows=0), "total_rows must be an integer of at least 1"),
        (table(ROW, total_rows=True), "total_rows must be an integer"),
        (table(ROW, total_rows=1.5), "total_rows must be an integer"),
        (table(*[ROW] * 101), "more than 100 rows"),
        ({"rows": "none"}, "open_prs.rows must be a list"),
        ({"total_rows": 1}, "open_prs.rows must be a list"),
        (table(ROW, extra=1), "rows and total_rows only"),
        ([ROW], "rows and total_rows only"),
        ("table", "rows and total_rows only"),
    ],
)
async def test_a_bad_table_answers_422_and_names_the_cell(
    async_client, async_session, automation, value, where
):
    response = await submit_table(async_client, automation, value)
    assert response.status_code == 422, response.text
    assert where in response.json()["detail"]
    stored = (
        await async_session.execute(AutomationOutcomeValue.__table__.select())
    ).all()
    assert stored == []


@pytest.mark.parametrize(
    "href",
    [
        "http://example.com/pr/1",
        "javascript:alert(1)",
        "data:text/html,x",
        "//example.com/pr/1",
        "/\\example.com",
        "https://",
        "https://example.com/a b",
        "https://example.com/\n",
        "pull/1",
        "",
        7,
    ],
)
async def test_an_unsafe_href_answers_422(async_client, automation, href):
    value = with_cell(0, {"v": "#1", "href": href})
    response = await submit_table(async_client, automation, value)
    assert response.status_code == 422, response.text
    assert "open_prs.rows[0][0].href" in response.json()["detail"]


@pytest.mark.parametrize("href", ["https://example.com/pr/1", "/pr/1", "/"])
async def test_an_https_url_or_a_path_is_a_valid_href(async_client, automation, href):
    value = with_cell(0, {"v": "#1", "href": href})
    response = await submit_table(async_client, automation, value)
    assert response.status_code == 201, response.text


async def test_a_flat_payload_cannot_write_a_table_key(async_client, automation):
    response = await async_client.post(
        SUBMIT,
        params={"automation_id": str(automation.id)},
        json={"payload": {"open_prs": "not a table"}},
    )
    assert response.status_code == 422, response.text
    assert "not registered with type 'string'" in response.json()["detail"]


async def test_a_json_key_without_a_shape_accepts_any_json(
    async_client,
    async_session,
    mock_authenticated_user,
    outcome_auth,  # noqa: F811
):
    factory = Factory(
        org_id=mock_authenticated_user.org_id,
        name="Free json",
        outcome_keys={"notes": {"type": "json"}},
    )
    async_session.add(factory)
    await async_session.commit()
    automation = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    for value in ({"anything": [1, {"x": None}]}, [1, 2], "text", 3):
        response = await async_client.post(
            SUBMIT,
            params={"automation_id": str(automation.id)},
            json={"values": [{"key": "notes", "type": "json", "value": value}]},
        )
        assert response.status_code == 201, response.text


async def test_the_shape_and_columns_are_fixed_once_the_key_has_facts(
    async_client, factory, automation
):
    url = f"{FACTORIES}/{factory}/outcome-keys/open_prs"
    changed_columns = key_with(columns=COLUMNS[:-1])

    assert (await async_client.put(url, json=TABLE_KEY)).status_code == 200
    assert (await async_client.put(url, json=changed_columns)).status_code == 200
    assert (await async_client.put(url, json=TABLE_KEY)).status_code == 200

    stored = await submit_table(async_client, automation, table(ROW))
    assert stored.status_code == 201, stored.text

    assert (await async_client.put(url, json=TABLE_KEY)).status_code == 200
    assert (await async_client.put(url, json=changed_columns)).status_code == 409
    assert (await async_client.put(url, json={"type": "json"})).status_code == 409


async def test_a_factory_can_be_created_with_a_table_key(async_client):
    created = await async_client.post(
        FACTORIES, json={"name": "Tables", "outcome_keys": {"open_prs": TABLE_KEY}}
    )
    assert created.status_code == 201, created.text
    assert created.json()["outcome_keys"]["open_prs"]["shape"] == "table.v1"


def test_the_validator_reads_columns_as_stored_without_defaults():
    stored = [{"name": "a", "type": "string"}, {"name": "b", "type": "integer"}]
    value = {"rows": [["x", 1], [None, None]]}
    assert validate_table_value(value, stored, "k") is value
    with pytest.raises(ValueError, match=r"k\.rows\[1\]\[1\] is not an integer"):
        validate_table_value({"rows": [["x", 1], ["y", "2"]]}, stored, "k")
