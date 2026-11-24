"""Factories: the factory API, assignment through PATCH, the list
filter, deletion, the change audit, and the Git Sync boundary.

API tests run on both PostgreSQL and SQLite: the count of live automations, the
delete transaction and the foreign key's SET NULL are SQL behavior.
"""

import asyncio
import uuid

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from openhands.automation.app import app
from openhands.automation.auth import (
    _get_local_user,
    authenticate_request,
    create_http_client,
)
from openhands.automation.db import using_sqlite
from openhands.automation.factory_router import lock_factory_for_assignment
from openhands.automation.git_sync.loop import (
    _create_automation_from_git,
    _Owner,
    _update_automation_from_git,
)
from openhands.automation.git_sync.serializer import (
    deserialize_automation,
    serialize_automation,
)
from openhands.automation.models import (
    Automation,
    AutomationChangeEvent,
    AutomationGitSyncState,
    AutomationOutcomeValue,
    Factory,
)
from openhands.automation.utils import utcnow


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BASE = "/api/automation/v1"
FACTORIES = f"{BASE}/factories"


@pytest.fixture
async def async_engine(dual_db_engine: AsyncEngine) -> AsyncEngine:
    return dual_db_engine


pytestmark = pytest.mark.usefixtures("local_file_store")


def _raw_body(name: str, **extra) -> dict:
    return {
        "name": name,
        "entrypoint": "python main.py",
        "tarball_path": "https://example.com/automation.tar.gz",
        "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        **extra,
    }


async def _create_automation(client: AsyncClient, name: str, **extra) -> dict:
    response = await client.post(BASE, json=_raw_body(name, **extra))
    assert response.status_code == 201, response.text
    return response.json()


async def _create_factory(client: AsyncClient, name: str, **extra) -> dict:
    response = await client.post(FACTORIES, json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _automation_row(name: str, **overrides) -> Automation:
    values = dict(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name=name,
        trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        tarball_path="https://example.com/automation.tar.gz",
        entrypoint="python main.py",
    )
    values.update(overrides)
    return Automation(**values)


# --- Create ---


async def test_fresh_install_has_no_factories(async_client):
    response = await async_client.get(FACTORIES)

    assert response.status_code == 200, response.text
    assert response.json() == {"factories": [], "total": 0}


async def test_create_returns_the_factory(async_client):
    data = await _create_factory(async_client, "Software Factory")

    assert data["name"] == "Software Factory"
    assert data["automation_count"] == 0
    assert uuid.UUID(data["id"])
    assert {"created_at", "updated_at"} <= data.keys()
    assert "slug" not in data
    assert "description" not in data


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "slug": "a-slug"},
        {"name": "   "},
        {"name": "x", "description": "no such attribute"},
        {},
    ],
)
async def test_create_rejects_invalid_input(async_client, body):
    response = await async_client.post(FACTORIES, json=body)
    assert response.status_code == 422, response.text


async def test_factory_names_need_not_be_unique(async_client):
    first = await _create_factory(async_client, "Software Factory")
    second = await _create_factory(async_client, "software factory")

    assert first["id"] != second["id"]
    assert (await async_client.get(FACTORIES)).json()["total"] == 2


# --- Read, rename ---


async def test_get_returns_the_factory_by_id(async_client):
    created = await _create_factory(async_client, "Software Factory")

    response = await async_client.get(f"{FACTORIES}/{created['id']}")

    assert response.status_code == 200, response.text
    assert response.json() == created


async def test_get_unknown_factory_is_404(async_client):
    response = await async_client.get(f"{FACTORIES}/{uuid.uuid4()}")
    assert response.status_code == 404, response.text


async def test_non_uuid_factory_id_is_422(async_client):
    response = await async_client.get(f"{FACTORIES}/software-factory")
    assert response.status_code == 422, response.text


async def test_other_orgs_factory_is_invisible(async_client, async_session):
    theirs = Factory(org_id=OTHER_ORG_ID, name="Theirs")
    async_session.add(theirs)
    await async_session.commit()
    url = f"{FACTORIES}/{theirs.id}"

    assert (await async_client.get(url)).status_code == 404
    assert (await async_client.patch(url, json={"name": "Mine"})).status_code == 404
    assert (await async_client.delete(url)).status_code == 404
    assert (await async_client.get(FACTORIES)).json()["total"] == 0


async def test_rename_changes_the_name(async_client):
    created = await _create_factory(async_client, "Software Factory")

    response = await async_client.patch(
        f"{FACTORIES}/{created['id']}", json={"name": "Release Train"}
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["name"], data["id"]) == ("Release Train", created["id"])
    assert data["updated_at"] >= created["updated_at"]


async def test_rename_rejects_a_slug(async_client):
    created = await _create_factory(async_client, "Software Factory")
    url = f"{FACTORIES}/{created['id']}"

    response = await async_client.patch(url, json={"name": "New", "slug": "new"})

    assert response.status_code == 422, response.text
    assert (await async_client.get(url)).json()["name"] == "Software Factory"


async def test_list_pages_over_factories(async_client):
    for name in ("One", "Two", "Three"):
        await _create_factory(async_client, name)

    response = await async_client.get(FACTORIES, params={"limit": 2, "offset": 2})

    data = response.json()
    assert (data["total"], len(data["factories"])) == (3, 1)


# --- automation_count ---


async def test_automation_count_includes_only_live_automations(
    async_client, async_session
):
    factory = await _create_factory(async_client, "Software Factory")
    factory_id = uuid.UUID(factory["id"])
    async_session.add_all(
        [
            _automation_row("Live one", factory_id=factory_id),
            _automation_row("Live two", factory_id=factory_id),
            _automation_row("Deleted", factory_id=factory_id, deleted_at=utcnow()),
            _automation_row("Elsewhere"),
        ]
    )
    await async_session.commit()

    one = await async_client.get(f"{FACTORIES}/{factory_id}")
    listed = await async_client.get(FACTORIES)

    assert one.json()["automation_count"] == 2
    assert [f["automation_count"] for f in listed.json()["factories"]] == [2]


# --- Assignment ---


async def test_existing_automation_has_no_factory(async_client):
    created = await _create_automation(async_client, "Plain")
    fetched = await async_client.get(f"{BASE}/{created['id']}")

    assert created["factory_id"] is None
    assert fetched.json()["factory_id"] is None


async def test_assign_move_and_clear_a_factory(async_client):
    first = await _create_factory(async_client, "First")
    second = await _create_factory(async_client, "Second")
    automation = await _create_automation(async_client, "Member")
    url = f"{BASE}/{automation['id']}"

    assigned = await async_client.patch(url, json={"factory_id": first["id"]})
    moved = await async_client.patch(url, json={"factory_id": second["id"]})
    renamed = await async_client.patch(url, json={"name": "Renamed"})
    cleared = await async_client.patch(url, json={"factory_id": None})

    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["factory_id"] == first["id"]
    assert moved.json()["factory_id"] == second["id"]
    assert renamed.json()["factory_id"] == second["id"]
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["factory_id"] is None
    assert (await async_client.get(url)).json()["factory_id"] is None


async def test_assign_responses_show_up_in_list_and_get(async_client):
    factory = await _create_factory(async_client, "First")
    automation = await _create_automation(async_client, "Member")
    await async_client.patch(
        f"{BASE}/{automation['id']}", json={"factory_id": factory["id"]}
    )

    fetched = await async_client.get(f"{BASE}/{automation['id']}")
    listed = await async_client.get(BASE)

    assert fetched.json()["factory_id"] == factory["id"]
    assert [a["factory_id"] for a in listed.json()["automations"]] == [factory["id"]]


async def test_assign_to_an_unknown_factory_is_404(async_client):
    automation = await _create_automation(async_client, "Member")

    response = await async_client.patch(
        f"{BASE}/{automation['id']}", json={"factory_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404, response.text


async def test_assign_to_another_orgs_factory_is_404(async_client, async_session):
    foreign = Factory(org_id=OTHER_ORG_ID, name="Theirs")
    async_session.add(foreign)
    await async_session.commit()
    automation = await _create_automation(async_client, "Member")

    response = await async_client.patch(
        f"{BASE}/{automation['id']}", json={"factory_id": str(foreign.id)}
    )

    assert response.status_code == 404, response.text
    fetched = await async_client.get(f"{BASE}/{automation['id']}")
    assert fetched.json()["factory_id"] is None


async def test_creator_without_manage_automations_cannot_change_the_factory(
    readonly_client, async_session
):
    factory = Factory(org_id=TEST_ORG_ID, name="First")
    async_session.add(factory)
    await async_session.flush()
    mine = _automation_row("Mine", user_id=TEST_USER_ID)
    in_factory = _automation_row("Grouped", factory_id=factory.id)
    async_session.add_all([mine, in_factory])
    await async_session.commit()

    assigned = await readonly_client.patch(
        f"{BASE}/{mine.id}", json={"factory_id": str(factory.id)}
    )
    cleared = await readonly_client.patch(
        f"{BASE}/{in_factory.id}", json={"factory_id": None}
    )
    renamed = await readonly_client.patch(f"{BASE}/{mine.id}", json={"name": "Ok"})

    assert assigned.status_code == 403, assigned.text
    assert cleared.status_code == 403, cleared.text
    assert renamed.status_code == 200, renamed.text
    await async_session.refresh(mine)
    await async_session.refresh(in_factory)
    assert (mine.factory_id, in_factory.factory_id) == (None, factory.id)


async def test_member_can_clear_no_factory_on_their_own_automation(
    readonly_client,
):
    created = await readonly_client.post(BASE, json=_raw_body("Mine", factory_id=None))
    assert created.status_code == 201, created.text

    cleared = await readonly_client.patch(
        f"{BASE}/{created.json()['id']}", json={"factory_id": None}
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["factory_id"] is None


async def test_resending_the_stored_factory_is_not_a_change(async_client):
    factory = await _create_factory(async_client, "First")
    automation = await _create_automation(async_client, "Member")
    url = f"{BASE}/{automation['id']}"
    await async_client.patch(url, json={"factory_id": factory["id"]})
    before = (await async_client.get(f"{url}/changes")).json()

    resent = await async_client.patch(
        url, json={"factory_id": factory["id"], "name": "Renamed"}
    )

    assert resent.status_code == 200, resent.text
    assert resent.json()["factory_id"] == factory["id"]
    after = (await async_client.get(f"{url}/changes")).json()
    assert [
        "factory_id" in change["changed_fields"] for change in after["changes"]
    ].count(True) == [
        "factory_id" in change["changed_fields"] for change in before["changes"]
    ].count(True)


async def test_member_cannot_edit_an_automation_in_a_factory(
    readonly_client, async_session
):
    factory = Factory(org_id=TEST_ORG_ID, name="First")
    async_session.add(factory)
    await async_session.flush()
    grouped = _automation_row("Grouped", user_id=TEST_USER_ID, factory_id=factory.id)
    async_session.add(grouped)
    await async_session.commit()

    for edit in (
        {"name": "Renamed"},
        {"entrypoint": "python evil.py"},
        {"tarball_path": "https://example.com/other.tar.gz"},
        {"factory_id": str(factory.id)},
    ):
        response = await readonly_client.patch(f"{BASE}/{grouped.id}", json=edit)
        assert response.status_code == 403, (edit, response.text)

    await async_session.refresh(grouped)
    assert (grouped.name, grouped.entrypoint) == ("Grouped", "python main.py")


async def test_admin_can_edit_an_automation_in_a_factory(async_client):
    factory = await _create_factory(async_client, "First")
    automation = await _create_automation(
        async_client, "Grouped", factory_id=factory["id"]
    )

    response = await async_client.patch(
        f"{BASE}/{automation['id']}", json={"name": "Renamed"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"


async def test_parsers_must_match_factory_registry_on_create_and_update(async_client):
    factory = await _create_factory(
        async_client, "Parser factory", outcome_keys={"score": "number"}
    )
    parser = {"key": "score", "expression": "result.score", "value_type": "number"}
    response = await async_client.post(
        BASE, json=_raw_body("No factory", outcome_parsers=[parser])
    )
    assert response.status_code == 409

    invalid = {**parser, "value_type": "string"}
    response = await async_client.post(
        BASE,
        json=_raw_body(
            "Wrong parser", factory_id=factory["id"], outcome_parsers=[invalid]
        ),
    )
    assert response.status_code == 422

    automation = await _create_automation(
        async_client, "Parsed", factory_id=factory["id"], outcome_parsers=[parser]
    )
    assert automation["outcome_parsers"] == [parser]
    url = f"{BASE}/{automation['id']}"
    response = await async_client.patch(url, json={"outcome_parsers": [invalid]})
    assert response.status_code == 422
    response = await async_client.patch(url, json={"factory_id": None})
    assert response.status_code == 409
    assert (await async_client.get(url)).json()["outcome_parsers"] == [parser]

    optional = {**parser, "required": False}
    created = await _create_automation(
        async_client, "Optional", factory_id=factory["id"], outcome_parsers=[optional]
    )
    assert created["outcome_parsers"] == [optional]
    response = await async_client.patch(url, json={"outcome_parsers": [optional]})
    assert response.status_code == 200, response.text
    assert (await async_client.get(url)).json()["outcome_parsers"] == [optional]


async def test_each_factory_change_appears_in_the_change_audit(
    async_client, async_session
):
    factory = await _create_factory(async_client, "First")
    automation = await _create_automation(async_client, "Member")
    url = f"{BASE}/{automation['id']}"

    await async_client.patch(url, json={"factory_id": factory["id"]})
    await async_client.patch(url, json={"factory_id": None})

    changes = await async_session.scalars(
        select(AutomationChangeEvent)
        .where(AutomationChangeEvent.automation_id == uuid.UUID(automation["id"]))
        .order_by(AutomationChangeEvent.created_at)
    )
    fields = [event.changed_fields for event in changes]
    assert fields[-2:] == [["factory_id"], ["factory_id"]]
    assert len(fields) == 3


# --- List filter ---


async def test_list_filters_by_factory(async_client):
    first = await _create_factory(async_client, "First")
    second = await _create_factory(async_client, "Second")
    for name, factory in (("A", first), ("B", first), ("C", second), ("D", None)):
        created = await _create_automation(async_client, name, tags=["qa"])
        if factory:
            await async_client.patch(
                f"{BASE}/{created['id']}", json={"factory_id": factory["id"]}
            )

    response = await async_client.get(BASE, params={"factory_id": first["id"]})
    combined = await async_client.get(
        BASE, params={"factory_id": second["id"], "tag": "qa", "created_by": "me"}
    )
    other = await async_client.get(BASE, params={"factory_id": str(uuid.uuid4())})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert sorted(a["name"] for a in response.json()["automations"]) == ["A", "B"]
    assert [a["name"] for a in combined.json()["automations"]] == ["C"]
    assert other.json() == {"automations": [], "total": 0}


async def test_list_filter_excludes_soft_deleted_automations(
    async_client, async_session
):
    factory = await _create_factory(async_client, "First")
    async_session.add(
        _automation_row(
            "Deleted", factory_id=uuid.UUID(factory["id"]), deleted_at=utcnow()
        )
    )
    await async_session.commit()

    response = await async_client.get(BASE, params={"factory_id": factory["id"]})

    assert response.json() == {"automations": [], "total": 0}


# --- Delete ---


async def test_delete_with_live_automations_is_409_and_states_the_count(
    async_client,
):
    factory = await _create_factory(async_client, "First")
    for name in ("A", "B"):
        created = await _create_automation(async_client, name)
        await async_client.patch(
            f"{BASE}/{created['id']}", json={"factory_id": factory["id"]}
        )

    response = await async_client.delete(f"{FACTORIES}/{factory['id']}")

    assert response.status_code == 409, response.text
    assert "2 live automations" in response.json()["detail"]
    assert (await async_client.get(f"{FACTORIES}/{factory['id']}")).status_code == 200


async def test_delete_succeeds_once_the_automations_are_gone_from_the_factory(
    async_client, async_session
):
    factory = await _create_factory(async_client, "First")
    kept = await _create_automation(async_client, "Kept")
    removed = await _create_automation(async_client, "Removed")
    for automation in (kept, removed):
        await async_client.patch(
            f"{BASE}/{automation['id']}", json={"factory_id": factory["id"]}
        )
    await async_client.patch(f"{BASE}/{kept['id']}", json={"factory_id": None})
    assert (await async_client.delete(f"{BASE}/{removed['id']}")).status_code == 204

    response = await async_client.delete(f"{FACTORIES}/{factory['id']}")

    assert response.status_code == 204, response.text
    assert (await async_client.get(f"{FACTORIES}/{factory['id']}")).status_code == 404
    assert (await async_client.get(FACTORIES)).json()["total"] == 0
    row = await async_session.scalar(
        select(Automation).where(Automation.id == uuid.UUID(removed["id"]))
    )
    assert row is not None
    await async_session.refresh(row)
    assert (row.deleted_at is not None, row.factory_id) == (True, None)


@pytest.fixture
async def race_client(dual_db_production_engine: AsyncEngine, mock_authenticated_user):
    """A client whose requests each get their own session, as in production.

    The shared ``async_client`` session serializes requests, so it cannot show a
    race.
    """
    app.state.engine = dual_db_production_engine
    app.state.session_factory = async_sessionmaker(
        dual_db_production_engine, class_=AsyncSession, expire_on_commit=False
    )
    app.state.http_client = create_http_client()
    app.dependency_overrides[authenticate_request] = lambda: mock_authenticated_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        await app.state.http_client.aclose()
        app.dependency_overrides.pop(authenticate_request, None)


async def test_delete_racing_an_assignment_never_orphans_a_live_automation(
    race_client,
):
    for round_number in range(20):
        factory = await _create_factory(race_client, f"Race {round_number}")
        automation = await _create_automation(race_client, f"Member {round_number}")

        deleted, assigned = await asyncio.gather(
            race_client.delete(f"{FACTORIES}/{factory['id']}"),
            race_client.patch(
                f"{BASE}/{automation['id']}", json={"factory_id": factory["id"]}
            ),
        )

        outcome = (deleted.status_code, assigned.status_code)
        assert outcome in {(204, 404), (409, 200)}, (deleted.text, assigned.text)
        member = (await race_client.get(f"{BASE}/{automation['id']}")).json()
        assert member["factory_id"] == (factory["id"] if outcome[0] == 409 else None)


async def test_an_assignment_in_flight_blocks_delete_but_not_rename(race_client):
    if using_sqlite():
        pytest.skip("PostgreSQL row locks")
    factory = await _create_factory(race_client, "First")
    url = f"{FACTORIES}/{factory['id']}"

    async with app.state.session_factory() as assigning:
        await lock_factory_for_assignment(
            assigning, TEST_ORG_ID, uuid.UUID(factory["id"])
        )
        renamed = await asyncio.wait_for(
            race_client.patch(url, json={"name": "Renamed"}), timeout=5
        )
        delete = asyncio.create_task(race_client.delete(url))
        finished, _ = await asyncio.wait({delete}, timeout=1)
        blocked = not finished
        await assigning.rollback()

    assert renamed.status_code == 200, renamed.text
    assert blocked
    assert (await delete).status_code == 204


async def test_deleted_factory_detach_is_audited(async_client, async_session):
    factory = await _create_factory(async_client, "First")
    automation = await _create_automation(async_client, "Member")
    await async_client.patch(
        f"{BASE}/{automation['id']}", json={"factory_id": factory["id"]}
    )
    await async_client.delete(f"{BASE}/{automation['id']}")
    await async_client.delete(f"{FACTORIES}/{factory['id']}")

    changes = await async_session.scalars(
        select(AutomationChangeEvent)
        .where(AutomationChangeEvent.automation_id == uuid.UUID(automation["id"]))
        .order_by(AutomationChangeEvent.created_at)
    )
    assert [e.changed_fields for e in changes][-1] == ["factory_id"]


async def test_factory_id_is_not_reused_after_delete(async_client):
    first = await _create_factory(async_client, "First")
    await async_client.delete(f"{FACTORIES}/{first['id']}")

    again = await _create_factory(async_client, "First")

    assert again["id"] != first["id"]


# --- Creating an automation in a factory ---

CRON = {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"}
CREATE_ENDPOINTS = {
    "raw": (BASE, lambda extra: _raw_body("In factory", **extra)),
    "prompt": (
        f"{BASE}/preset/prompt",
        lambda extra: {
            "name": "In factory",
            "prompt": "Say hi",
            "trigger": CRON,
            **extra,
        },
    ),
    "plugin": (
        f"{BASE}/preset/plugin",
        lambda extra: {
            "name": "In factory",
            "prompt": "Say hi",
            "plugins": [{"source": "github:owner/plugin"}],
            "trigger": CRON,
            **extra,
        },
    ),
    "plugin-spec": (
        f"{BASE}/plugin-spec",
        lambda extra: {
            "name": "In factory",
            "plugin": {"source": "github:owner/plugin"},
            "trigger": CRON,
            **extra,
        },
    ),
}
# plugin-spec needs a fetchable manifest to succeed; the factory checks run first.
CREATABLE = ["raw", "prompt", "plugin"]


@pytest.mark.parametrize("endpoint", CREATABLE)
async def test_create_endpoint_assigns_the_factory(async_client, endpoint):
    factory = await _create_factory(async_client, "First")
    url, build = CREATE_ENDPOINTS[endpoint]

    response = await async_client.post(url, json=build({"factory_id": factory["id"]}))

    assert response.status_code == 201, response.text
    assert response.json()["factory_id"] == factory["id"]
    fetched = await async_client.get(FACTORIES + f"/{factory['id']}")
    assert fetched.json()["automation_count"] == 1


@pytest.mark.parametrize("endpoint", CREATE_ENDPOINTS)
async def test_create_endpoint_unknown_factory_is_404(async_client, endpoint):
    url, build = CREATE_ENDPOINTS[endpoint]

    response = await async_client.post(
        url, json=build({"factory_id": str(uuid.uuid4())})
    )

    assert response.status_code == 404, response.text


@pytest.mark.parametrize("endpoint", CREATE_ENDPOINTS)
async def test_create_endpoint_other_orgs_factory_is_404(
    async_client, async_session, endpoint
):
    foreign = Factory(org_id=OTHER_ORG_ID, name="Theirs")
    async_session.add(foreign)
    await async_session.commit()
    url, build = CREATE_ENDPOINTS[endpoint]

    response = await async_client.post(url, json=build({"factory_id": str(foreign.id)}))

    assert response.status_code == 404, response.text
    assert (await async_client.get(BASE)).json()["total"] == 0


@pytest.mark.parametrize("endpoint", CREATE_ENDPOINTS)
async def test_create_endpoint_member_cannot_set_a_factory(
    readonly_client, async_session, endpoint
):
    factory = Factory(org_id=TEST_ORG_ID, name="First")
    async_session.add(factory)
    await async_session.commit()
    url, build = CREATE_ENDPOINTS[endpoint]

    response = await readonly_client.post(
        url, json=build({"factory_id": str(factory.id)})
    )

    assert response.status_code == 403, response.text


@pytest.mark.parametrize("endpoint", CREATABLE)
async def test_member_can_still_create_without_a_factory(readonly_client, endpoint):
    url, build = CREATE_ENDPOINTS[endpoint]

    response = await readonly_client.post(url, json=build({}))

    assert response.status_code == 201, response.text
    assert response.json()["factory_id"] is None


TEMPLATE = {"id": "tmpl-1", "version": "1"}


@pytest.mark.parametrize("endpoint", CREATE_ENDPOINTS)
@pytest.mark.parametrize("extra", ["omitted", "same"])
async def test_template_reenable_returns_the_existing_automation_when_factory_matches(
    async_client, endpoint, extra
):
    factory = await _create_factory(async_client, "First")
    existing = await _create_automation(
        async_client, "Installed", template=TEMPLATE, factory_id=factory["id"]
    )
    url, build = CREATE_ENDPOINTS[endpoint]
    body = {"template": TEMPLATE}
    if extra == "same":
        body["factory_id"] = factory["id"]

    response = await async_client.post(url, json=build(body))

    assert response.status_code == 200, response.text
    assert response.json()["id"] == existing["id"]
    assert response.json()["factory_id"] == factory["id"]


@pytest.mark.parametrize("endpoint", CREATE_ENDPOINTS)
@pytest.mark.parametrize("installed_in", ["another factory", "no factory"])
async def test_template_reenable_asking_for_a_different_factory_is_409(
    async_client, endpoint, installed_in
):
    first = await _create_factory(async_client, "First")
    second = await _create_factory(async_client, "Second")
    installed_extra = (
        {"factory_id": first["id"]} if installed_in == "another factory" else {}
    )
    existing = await _create_automation(
        async_client, "Installed", template=TEMPLATE, **installed_extra
    )
    url, build = CREATE_ENDPOINTS[endpoint]

    response = await async_client.post(
        url, json=build({"template": TEMPLATE, "factory_id": second["id"]})
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert existing["id"] in detail
    assert (str(first["id"]) in detail) == (installed_in == "another factory")
    unchanged = (await async_client.get(f"{BASE}/{existing['id']}")).json()
    assert unchanged["factory_id"] == installed_extra.get("factory_id")
    assert (await async_client.get(f"{FACTORIES}/{second['id']}")).json()[
        "automation_count"
    ] == 0


# --- Draft validation ---


async def _validate(client: AsyncClient, factory_id) -> dict:
    response = await client.post(
        f"{BASE}/validate",
        json={"endpoint": "/v1", "draft": _raw_body("Draft", factory_id=factory_id)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _factory_error_codes(result: dict) -> list[str]:
    return [e["code"] for e in result["errors"] if e["field"] == "factory_id"]


async def test_validate_accepts_a_usable_factory_and_no_factory(async_client):
    factory = await _create_factory(async_client, "First")

    assert _factory_error_codes(await _validate(async_client, factory["id"])) == []
    assert _factory_error_codes(await _validate(async_client, None)) == []


async def test_validate_reports_an_unknown_or_other_org_factory(
    async_client, async_session
):
    foreign = Factory(org_id=OTHER_ORG_ID, name="Theirs")
    async_session.add(foreign)
    await async_session.commit()

    unknown = await _validate(async_client, str(uuid.uuid4()))
    other_org = await _validate(async_client, str(foreign.id))

    for result in (unknown, other_org):
        assert result["valid"] is False
        assert _factory_error_codes(result) == ["factory_id_404"]


async def test_validate_reports_a_member_setting_a_factory(
    readonly_client, async_session
):
    factory = Factory(org_id=TEST_ORG_ID, name="First")
    async_session.add(factory)
    await async_session.commit()

    result = await _validate(readonly_client, str(factory.id))

    assert result["valid"] is False
    assert _factory_error_codes(result) == ["factory_id_403"]


async def test_draft_with_an_unusable_factory_is_not_dispatchable(async_client):
    created = await async_client.post(
        f"{BASE}/drafts",
        json={
            "endpoint": "/v1",
            "draft": _raw_body("Draft", factory_id=str(uuid.uuid4())),
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["dispatchable"] is False
    assert [e["code"] for e in created.json()["validation_errors"]] == [
        "factory_id_404"
    ]


async def test_draft_becomes_dispatchable_when_its_factory_exists(async_client):
    factory = await _create_factory(async_client, "First")

    created = await async_client.post(
        f"{BASE}/drafts",
        json={"endpoint": "/v1", "draft": _raw_body("Draft", factory_id=factory["id"])},
    )

    assert created.status_code == 201, created.text
    assert created.json()["dispatchable"] is True
    assert created.json()["validation_errors"] is None


async def test_draft_keeps_factory_id_but_its_test_run_has_none(
    async_client, async_session
):
    factory = await _create_factory(async_client, "First")
    created = await async_client.post(
        f"{BASE}/drafts",
        json={
            "endpoint": "/v1",
            "draft": _raw_body("Draft", factory_id=factory["id"]),
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["draft"]["factory_id"] == factory["id"]

    dispatched = await async_client.post(
        f"{BASE}/drafts/{created.json()['id']}/dispatch"
    )

    assert dispatched.status_code == 201, dispatched.text
    hidden = await async_session.get(
        Automation, uuid.UUID(dispatched.json()["automation_id"])
    )
    assert hidden is not None and hidden.factory_id is None
    deleted = await async_client.delete(f"{FACTORIES}/{factory['id']}")
    assert deleted.status_code == 204, deleted.text


async def test_draft_test_run_automation_cannot_be_moved_into_a_factory(
    async_client,
):
    factory = await _create_factory(async_client, "First")
    created = await async_client.post(
        f"{BASE}/drafts", json={"endpoint": "/v1", "draft": _raw_body("Draft")}
    )
    dispatched = await async_client.post(
        f"{BASE}/drafts/{created.json()['id']}/dispatch"
    )
    hidden_id = dispatched.json()["automation_id"]

    response = await async_client.patch(
        f"{BASE}/{hidden_id}", json={"factory_id": factory["id"]}
    )

    assert response.status_code == 409, response.text
    assert (await async_client.get(f"{BASE}/{hidden_id}")).json()["factory_id"] is None
    deleted = await async_client.delete(f"{FACTORIES}/{factory['id']}")
    assert deleted.status_code == 204, deleted.text


# --- Authorization ---


async def test_member_can_read_but_not_write_factories(readonly_client, async_session):
    factory = Factory(org_id=TEST_ORG_ID, name="First")
    async_session.add(factory)
    await async_session.commit()
    url = f"{FACTORIES}/{factory.id}"

    assert (await readonly_client.get(FACTORIES)).status_code == 200
    assert (await readonly_client.get(url)).status_code == 200
    create = await readonly_client.post(FACTORIES, json={"name": "New"})
    rename = await readonly_client.patch(url, json={"name": "New"})
    delete = await readonly_client.delete(url)
    assert (create.status_code, rename.status_code, delete.status_code) == (
        403,
        403,
        403,
    )


async def test_member_can_list_a_factorys_automations(readonly_client, async_session):
    factory = Factory(org_id=TEST_ORG_ID, name="First")
    async_session.add(factory)
    await async_session.flush()
    async_session.add(_automation_row("Grouped", factory_id=factory.id))
    await async_session.commit()

    response = await readonly_client.get(BASE, params={"factory_id": str(factory.id)})

    assert response.status_code == 200, response.text
    assert [a["name"] for a in response.json()["automations"]] == ["Grouped"]


def test_local_api_key_holds_manage_automations():
    assert "manage_automations" in _get_local_user().permissions


# --- Git Sync ---


def test_git_sync_export_does_not_write_the_factory():
    automation = _automation_row("Synced", id=uuid.uuid4(), factory_id=uuid.uuid4())

    files = serialize_automation(automation, None)

    assert "factory" not in files["automation.yaml"].decode()
    assert "factory_id" not in yaml.safe_load(files["automation.yaml"].decode())


async def test_git_sync_update_leaves_the_factory_unchanged(
    async_session: AsyncSession,
):
    factory = Factory(
        org_id=TEST_ORG_ID, name="First", outcome_keys={"score": "number"}
    )
    async_session.add(factory)
    await async_session.flush()
    files = serialize_automation(_automation_row("Synced", id=uuid.uuid4()), None)
    deserialized = deserialize_automation(files)
    assert deserialized is not None
    await _create_automation_from_git(
        async_session,
        _Owner(TEST_USER_ID, TEST_ORG_ID),
        "synced",
        deserialized,
        files,
        "head-1",
        [],
    )
    await async_session.flush()
    automation = await async_session.scalar(select(Automation))
    state = await async_session.scalar(select(AutomationGitSyncState))
    assert automation is not None and state is not None
    automation.factory_id = factory.id
    await async_session.flush()

    changed = yaml.safe_load(files["automation.yaml"].decode())
    changed["name"] = "Renamed in git"
    changed["outcome_parsers"] = [
        {"key": "score", "expression": "result.score", "value_type": "number"}
    ]
    changed_files = {**files, "automation.yaml": yaml.safe_dump(changed).encode()}
    changed_deserialized = deserialize_automation(changed_files)
    assert changed_deserialized is not None
    await _update_automation_from_git(
        async_session, state, changed_deserialized, changed_files, "head-2", []
    )
    await async_session.flush()
    await async_session.refresh(automation)

    assert automation.name == "Renamed in git"
    assert automation.factory_id == factory.id

    assert automation.outcome_parsers == changed["outcome_parsers"]
    exported = serialize_automation(automation, None)
    assert (
        yaml.safe_load(exported["automation.yaml"])["outcome_parsers"]
        == changed["outcome_parsers"]
    )


# --- outcome keys ---


async def test_outcome_keys_are_managed_as_subresource(async_client):
    created = await _create_factory(async_client, "Software Factory")
    url = f"{FACTORIES}/{created['id']}"

    assert (await async_client.get(f"{url}/outcome-keys")).json() == {}

    added = await async_client.put(f"{url}/outcome-keys/score", json={"type": "number"})
    assert added.status_code == 200, added.text
    assert added.json()["outcome_keys"] == {"score": "number"}
    assert (await async_client.get(f"{url}/outcome-keys")).json() == {"score": "number"}

    renamed = await async_client.patch(url, json={"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["outcome_keys"] == {"score": "number"}

    rejected = await async_client.patch(
        url, json={"name": "Renamed", "outcome_keys": {"score": "string"}}
    )
    assert rejected.status_code == 422, rejected.text

    removed = await async_client.delete(f"{url}/outcome-keys/score")
    assert removed.status_code == 204, removed.text
    assert (await async_client.get(f"{url}/outcome-keys")).json() == {}


async def test_used_outcome_key_cannot_be_retyped_or_removed(
    async_client, async_session
):
    created = await _create_factory(
        async_client, "Software Factory", outcome_keys={"score": "number"}
    )
    factory_id = uuid.UUID(created["id"])
    automation = _automation_row("Member", factory_id=factory_id)
    async_session.add(automation)
    await async_session.flush()
    async_session.add(
        AutomationOutcomeValue(
            org_id=TEST_ORG_ID,
            factory_id=factory_id,
            automation_id=automation.id,
            source="user",
            key="score",
            value_type="number",
            numeric_value=1.0,
        )
    )
    await async_session.commit()
    url = f"{FACTORIES}/{created['id']}/outcome-keys/score"

    retyped = await async_client.put(url, json={"type": "string"})
    removed = await async_client.delete(url)

    assert retyped.status_code == 409, retyped.text
    assert removed.status_code == 409, removed.text
    assert (
        await async_client.get(f"{FACTORIES}/{created['id']}/outcome-keys")
    ).json() == {"score": "number"}
