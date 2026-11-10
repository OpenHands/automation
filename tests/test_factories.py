"""Factories: the CRUD API, assigning automations, deletion rules, permissions,
the audit trail, and the Git Sync boundary.

API tests run on both PostgreSQL and SQLite: the factory count and the
foreign key behave through different drivers on each.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from openhands.automation.db import set_sqlite_mode, using_sqlite
from openhands.automation.factory_router import derive_slug
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
    Base,
    Factory,
)


BASE = "/api/automation/v1"
FACTORIES = f"{BASE}/factories"


@pytest.fixture(params=["postgresql", "sqlite"])
async def async_engine(request, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Run each API test against both supported databases."""
    previous_sqlite_mode = using_sqlite()
    if request.param == "sqlite":
        set_sqlite_mode(True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/factories.db")
    else:
        container = request.getfixturevalue("postgres_container")
        engine = create_async_engine(
            container.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+asyncpg://"
            )
        )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()
        set_sqlite_mode(previous_sqlite_mode)


async def _factory(
    session: AsyncSession, org_id: uuid.UUID, name: str, slug: str | None = None
) -> Factory:
    factory = Factory(org_id=org_id, name=name, slug=slug or derive_slug(name))
    session.add(factory)
    await session.commit()
    return factory


async def _automation(
    session: AsyncSession, user, name: str = "Automation", **overrides
) -> Automation:
    values = dict(
        user_id=user.user_id,
        org_id=user.org_id,
        name=name,
        trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        tarball_path="https://example.com/automation.tar.gz",
        entrypoint="python main.py",
    )
    values.update(overrides)
    automation = Automation(**values)
    session.add(automation)
    await session.commit()
    return automation


async def _assign(
    client: AsyncClient, automation: Automation, factory_id: uuid.UUID | None
) -> Response:
    return await client.patch(
        f"{BASE}/{automation.id}",
        json={"factory_id": None if factory_id is None else str(factory_id)},
    )


# --- Slug derivation ---


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        pytest.param("Dark Factory", "dark-factory", id="words"),
        pytest.param("  Ünïcode & Co.  ", "unicode-co", id="accents-and-symbols"),
        pytest.param("a" * 100, "a" * 64, id="truncated"),
        pytest.param("x" * 63 + " y", "x" * 63, id="no-trailing-hyphen-after-cut"),
        pytest.param("!!!", "", id="nothing-to-derive"),
    ],
)
def test_derive_slug(name, slug):
    assert derive_slug(name) == slug


# --- Create, read, rename ---


async def test_fresh_install_has_no_factories(async_client):
    response = await async_client.get(FACTORIES)

    assert response.status_code == 200, response.text
    assert response.json() == {"factories": [], "total": 0}


async def test_create_derives_the_slug_and_starts_empty(async_client):
    response = await async_client.post(FACTORIES, json={"name": "Dark Factory"})

    assert response.status_code == 201, response.text
    data = response.json()
    assert (data["name"], data["slug"], data["automation_count"]) == (
        "Dark Factory",
        "dark-factory",
        0,
    )
    assert uuid.UUID(data["id"])
    assert data["created_at"] and data["updated_at"]


async def test_create_keeps_an_explicit_slug(async_client):
    response = await async_client.post(
        FACTORIES, json={"name": "Dark Factory", "slug": "dark-1"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["slug"] == "dark-1"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": "F", "slug": "Upper"}, id="uppercase-slug"),
        pytest.param({"name": "F", "slug": "under_score"}, id="underscore-slug"),
        pytest.param({"name": "F", "slug": "-lead"}, id="leading-hyphen-slug"),
        pytest.param({"name": "F", "slug": "a--b"}, id="double-hyphen-slug"),
        pytest.param({"name": "F", "slug": "x" * 65}, id="long-slug"),
        pytest.param({"name": "   "}, id="blank-name"),
        pytest.param({"name": "x" * 201}, id="long-name"),
        pytest.param({"name": "F", "description": "no"}, id="unknown-field"),
        pytest.param({"name": "!!!"}, id="slug-cannot-be-derived"),
    ],
)
async def test_create_rejects_invalid_input(async_client, body):
    response = await async_client.post(FACTORIES, json=body)

    assert response.status_code == 422, response.text


async def test_duplicate_slug_in_the_org_is_a_conflict(async_client):
    await async_client.post(FACTORIES, json={"name": "Dark Factory"})

    derived = await async_client.post(FACTORIES, json={"name": "Dark  factory"})
    explicit = await async_client.post(
        FACTORIES, json={"name": "Other", "slug": "dark-factory"}
    )

    assert (derived.status_code, explicit.status_code) == (409, 409)


async def test_same_slug_is_allowed_in_another_org(async_client, async_session):
    await _factory(async_session, uuid.uuid4(), "Dark Factory")

    response = await async_client.post(FACTORIES, json={"name": "Dark Factory"})

    assert response.status_code == 201, response.text


async def test_get_by_slug(async_client, async_session, mock_authenticated_user):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "Dark")

    response = await async_client.get(f"{FACTORIES}/dark")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(factory.id)


async def test_unknown_and_other_org_slugs_are_not_found(async_client, async_session):
    await _factory(async_session, uuid.uuid4(), "Elsewhere")

    unknown = await async_client.get(f"{FACTORIES}/nope")
    other_org = await async_client.get(f"{FACTORIES}/elsewhere")

    assert (unknown.status_code, other_org.status_code) == (404, 404)


async def test_rename_changes_the_name_and_never_the_slug(
    async_client, async_session, mock_authenticated_user
):
    await _factory(async_session, mock_authenticated_user.org_id, "Dark")

    renamed = await async_client.patch(f"{FACTORIES}/dark", json={"name": "Bright"})
    slug_change = await async_client.patch(
        f"{FACTORIES}/dark", json={"name": "Bright", "slug": "bright"}
    )

    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["slug"]) == ("Bright", "dark")
    assert slug_change.status_code == 422
    assert (await async_client.get(f"{FACTORIES}/dark")).json()["name"] == "Bright"


async def test_rename_unknown_factory_is_not_found(async_client):
    response = await async_client.patch(f"{FACTORIES}/nope", json={"name": "X"})

    assert response.status_code == 404


async def test_list_orders_by_name_pages_and_stays_in_the_org(
    async_client, async_session, mock_authenticated_user
):
    org_id = mock_authenticated_user.org_id
    for name in ("beta", "Alpha", "gamma"):
        await _factory(async_session, org_id, name)
    await _factory(async_session, uuid.uuid4(), "Elsewhere")

    page = await async_client.get(FACTORIES, params={"limit": 2})
    rest = await async_client.get(FACTORIES, params={"limit": 2, "offset": 2})

    assert page.json()["total"] == 3
    assert [f["name"] for f in page.json()["factories"]] == ["Alpha", "beta"]
    assert [f["name"] for f in rest.json()["factories"]] == ["gamma"]


# --- Assigning automations ---


async def test_assign_move_and_remove_an_automation(
    async_client, async_session, mock_authenticated_user
):
    org_id = mock_authenticated_user.org_id
    first = await _factory(async_session, org_id, "First")
    second = await _factory(async_session, org_id, "Second")
    automation = await _automation(async_session, mock_authenticated_user)
    unassigned = await async_client.get(f"{BASE}/{automation.id}")
    assert unassigned.json()["factory_id"] is None

    assigned = await _assign(async_client, automation, first.id)
    moved = await _assign(async_client, automation, second.id)
    removed = await _assign(async_client, automation, None)

    assert assigned.status_code == moved.status_code == removed.status_code == 200
    assert assigned.json()["factory_id"] == str(first.id)
    assert moved.json()["factory_id"] == str(second.id)
    assert removed.json()["factory_id"] is None


async def test_omitting_factory_id_leaves_it_unchanged(
    async_client, async_session, mock_authenticated_user
):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "F")
    automation = await _automation(
        async_session, mock_authenticated_user, factory_id=factory.id
    )

    response = await async_client.patch(
        f"{BASE}/{automation.id}", json={"name": "Renamed"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["factory_id"] == str(factory.id)


async def test_assigning_a_factory_of_another_org_or_none_is_not_found(
    async_client, async_session, mock_authenticated_user
):
    foreign = await _factory(async_session, uuid.uuid4(), "Foreign")
    automation = await _automation(async_session, mock_authenticated_user)

    other_org = await _assign(async_client, automation, foreign.id)
    missing = await _assign(async_client, automation, uuid.uuid4())

    assert (other_org.status_code, missing.status_code) == (404, 404)
    await async_session.refresh(automation)
    assert automation.factory_id is None


async def test_creator_without_manage_automations_cannot_change_the_factory(
    readonly_client, async_session, mock_authenticated_user
):
    org_id = mock_authenticated_user.org_id
    joined = await _factory(async_session, org_id, "Joined")
    other = await _factory(async_session, org_id, "Other")
    mine = await _automation(async_session, mock_authenticated_user, "Mine")
    member = await _automation(
        async_session, mock_authenticated_user, "Member", factory_id=joined.id
    )

    set_ = await _assign(readonly_client, mine, joined.id)
    moved = await _assign(readonly_client, member, other.id)
    cleared = await _assign(readonly_client, member, None)

    assert (set_.status_code, moved.status_code, cleared.status_code) == (403,) * 3
    await async_session.refresh(mine)
    await async_session.refresh(member)
    assert (mine.factory_id, member.factory_id) == (None, joined.id)


async def test_creator_can_edit_an_automation_that_keeps_its_factory(
    readonly_client, async_session, mock_authenticated_user
):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "F")
    automation = await _automation(
        async_session, mock_authenticated_user, factory_id=factory.id
    )

    response = await readonly_client.patch(
        f"{BASE}/{automation.id}",
        json={"name": "Renamed", "factory_id": str(factory.id)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"


async def test_factory_change_appears_in_the_change_audit(
    async_client, async_session, mock_authenticated_user
):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "F")
    automation = await _automation(async_session, mock_authenticated_user)

    await _assign(async_client, automation, factory.id)

    events = (
        await async_session.scalars(
            select(AutomationChangeEvent).where(
                AutomationChangeEvent.automation_id == automation.id
            )
        )
    ).all()
    assert any("factory_id" in event.changed_fields for event in events)


# --- Listing a factory's automations ---


async def test_list_filters_by_factory_and_counts_only_live_automations(
    async_client, async_session, mock_authenticated_user
):
    org_id = mock_authenticated_user.org_id
    user = mock_authenticated_user
    mine = await _factory(async_session, org_id, "Mine")
    other = await _factory(async_session, org_id, "Other")
    for name in ("One", "Two"):
        await _automation(async_session, user, name, factory_id=mine.id)
    gone = await _automation(async_session, user, "Gone", factory_id=mine.id)
    await _automation(async_session, user, "Elsewhere", factory_id=other.id)
    await _automation(async_session, user, "Loose")
    assert (await async_client.delete(f"{BASE}/{gone.id}")).status_code == 204

    listed = await async_client.get(BASE, params={"factory_id": str(mine.id)})
    counted = await async_client.get(f"{FACTORIES}/mine")

    assert sorted(a["name"] for a in listed.json()["automations"]) == ["One", "Two"]
    assert listed.json()["total"] == 2
    assert counted.json()["automation_count"] == 2


async def test_list_filter_combines_with_other_filters_and_stays_in_the_org(
    async_client, async_session, mock_authenticated_user
):
    user = mock_authenticated_user
    foreign = await _factory(async_session, uuid.uuid4(), "Foreign")
    factory = await _factory(async_session, user.org_id, "Mine")
    await _automation(async_session, user, "Tagged", factory_id=factory.id, tags=["qa"])
    await _automation(async_session, user, "Plain", factory_id=factory.id)
    await _automation(
        async_session,
        user,
        "Foreign's",
        org_id=foreign.org_id,
        factory_id=foreign.id,
    )

    combined = await async_client.get(
        BASE, params={"factory_id": str(factory.id), "tag": "qa"}
    )
    foreign_list = await async_client.get(BASE, params={"factory_id": str(foreign.id)})

    assert [a["name"] for a in combined.json()["automations"]] == ["Tagged"]
    assert foreign_list.json() == {"automations": [], "total": 0}


# --- Deletion ---


async def test_delete_is_a_conflict_while_a_live_automation_belongs(
    async_client, async_session, mock_authenticated_user
):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "F")
    for name in ("One", "Two"):
        await _automation(
            async_session, mock_authenticated_user, name, factory_id=factory.id
        )

    response = await async_client.delete(f"{FACTORIES}/f")

    assert response.status_code == 409, response.text
    assert "2 live automations" in response.json()["detail"]
    assert (await async_client.get(f"{FACTORIES}/f")).status_code == 200


async def test_delete_removes_the_factory_once_its_automations_are_removed(
    async_client, async_session, mock_authenticated_user
):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "F")
    automation = await _automation(
        async_session, mock_authenticated_user, factory_id=factory.id
    )
    await _assign(async_client, automation, None)

    response = await async_client.delete(f"{FACTORIES}/f")

    assert response.status_code == 204, response.text
    assert (await async_client.get(f"{FACTORIES}/f")).status_code == 404


async def test_delete_detaches_soft_deleted_automations(
    async_client, async_session, mock_authenticated_user
):
    factory = await _factory(async_session, mock_authenticated_user.org_id, "F")
    automation = await _automation(
        async_session, mock_authenticated_user, factory_id=factory.id
    )
    assert (await async_client.delete(f"{BASE}/{automation.id}")).status_code == 204

    response = await async_client.delete(f"{FACTORIES}/f")

    assert response.status_code == 204, response.text
    await async_session.refresh(automation)
    assert (automation.deleted_at is not None, automation.factory_id) == (True, None)


# --- Permissions ---


async def test_members_can_read_but_not_write_factories(
    readonly_client, async_session, mock_authenticated_user
):
    await _factory(async_session, mock_authenticated_user.org_id, "F")

    listed = await readonly_client.get(FACTORIES)
    fetched = await readonly_client.get(f"{FACTORIES}/f")
    created = await readonly_client.post(FACTORIES, json={"name": "New"})
    renamed = await readonly_client.patch(f"{FACTORIES}/f", json={"name": "X"})
    deleted = await readonly_client.delete(f"{FACTORIES}/f")

    assert (listed.status_code, fetched.status_code) == (200, 200)
    assert (created.status_code, renamed.status_code, deleted.status_code) == (403,) * 3


# --- Git Sync ---


async def test_git_sync_neither_writes_nor_reads_the_factory(
    async_session, mock_authenticated_user
):
    user = mock_authenticated_user
    factory = await _factory(async_session, user.org_id, "F")
    automation = await _automation(
        async_session, user, id=uuid.uuid4(), factory_id=factory.id
    )
    exported = yaml.safe_load(
        serialize_automation(automation, None)["automation.yaml"].decode()
    )
    assert "factory_id" not in exported

    files = {"automation.yaml": yaml.safe_dump(exported).encode()}
    deserialized = deserialize_automation(files)
    assert deserialized is not None
    async with async_session.begin_nested():
        await _create_automation_from_git(
            async_session,
            _Owner(user.user_id, user.org_id),
            "synced",
            deserialized,
            files,
            "head-1",
            [],
        )
    imported = (
        await async_session.scalars(
            select(Automation).where(Automation.id != automation.id)
        )
    ).one()
    imported.factory_id = factory.id
    state = await async_session.scalar(
        select(AutomationGitSyncState).where(
            AutomationGitSyncState.automation_id == imported.id
        )
    )
    assert state is not None

    changed_files = {
        "automation.yaml": yaml.safe_dump(
            {**exported, "name": "Renamed in git"}
        ).encode()
    }
    changed = deserialize_automation(changed_files)
    assert changed is not None
    await _update_automation_from_git(
        async_session, state, changed, changed_files, "head-2", []
    )

    assert (imported.name, imported.factory_id) == ("Renamed in git", factory.id)
