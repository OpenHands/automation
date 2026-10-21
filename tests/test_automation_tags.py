"""Automation tags: normalization, the list filter, the tag facet endpoint,
editing, and the Git Sync round trip.

API tests run on both PostgreSQL and SQLite, since the tag filter and facet
query expand the JSON column with different functions on each.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from openhands.automation.app import app
from openhands.automation.db import set_sqlite_mode, using_sqlite
from openhands.automation.git_sync.loop import _create_automation_from_git, _Owner
from openhands.automation.git_sync.serializer import (
    deserialize_automation,
    serialize_automation,
)
from openhands.automation.models import Automation, Base
from openhands.automation.storage import get_file_store
from openhands.automation.storage.local import LocalFileStore
from openhands.automation.tags import MAX_TAGS, normalize_tags
from openhands.automation.utils import utcnow


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BASE = "/api/automation/v1"


@pytest.fixture(params=["postgresql", "sqlite"])
async def async_engine(request, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Run each API test against both supported databases."""
    previous_sqlite_mode = using_sqlite()
    if request.param == "sqlite":
        set_sqlite_mode(True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/tags.db")
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


@pytest.fixture(autouse=True)
def file_store(tmp_path: Path):
    store = LocalFileStore(str(tmp_path / "storage"))
    app.dependency_overrides[get_file_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_file_store, None)


def _raw_body(name: str, **extra) -> dict:
    return {
        "name": name,
        "entrypoint": "python main.py",
        "tarball_path": "https://example.com/automation.tar.gz",
        "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        **extra,
    }


async def _create(client: AsyncClient, name: str, **extra) -> dict:
    response = await client.post(BASE, json=_raw_body(name, **extra))
    assert response.status_code == 201, response.text
    return response.json()


def _row(name: str, tags: list[str], **overrides) -> Automation:
    values = dict(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name=name,
        trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        tarball_path="https://example.com/automation.tar.gz",
        entrypoint="python main.py",
        tags=tags,
    )
    values.update(overrides)
    return Automation(**values)


# --- Normalization ---


def test_normalize_tags_slugs_and_dedupes_in_order():
    assert normalize_tags(["  Software Factory ", "software-factory", "QA"]) == [
        "software-factory",
        "qa",
    ]


@pytest.mark.parametrize(
    "tags",
    [
        pytest.param([""], id="empty"),
        pytest.param(["under_score"], id="underscore"),
        pytest.param(["-leading"], id="leading-hyphen"),
        pytest.param(["double--hyphen"], id="double-hyphen"),
        pytest.param(["x" * 51], id="too-long"),
        pytest.param([f"t{i}" for i in range(MAX_TAGS + 1)], id="too-many"),
        pytest.param([3], id="not-a-string"),
    ],
)
def test_normalize_tags_rejects_non_slugs(tags):
    with pytest.raises(ValueError):
        normalize_tags(tags)


# --- Create ---


async def test_create_normalizes_tags(async_client):
    data = await _create(async_client, "Reviewer", tags=["Software Factory", "QA"])
    assert data["tags"] == ["software-factory", "qa"]


async def test_create_without_tags_returns_empty_list(async_client):
    data = await _create(async_client, "Untagged")
    assert data["tags"] == []


async def test_create_rejects_invalid_tag(async_client):
    response = await async_client.post(
        BASE, json=_raw_body("Bad", tags=["not_a_slug"])
    )
    assert response.status_code == 422, response.text


async def test_template_create_returns_existing_tags_unchanged(async_client):
    template = {"id": "github-pr-reviewer", "version": "1"}
    first = await _create(async_client, "Reviewer", tags=["mine"], template=template)

    response = await async_client.post(
        BASE,
        json=_raw_body("Reviewer", tags=["software-factory"], template=template),
    )

    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["tags"]) == (
        first["id"],
        ["mine"],
    )


async def test_prompt_preset_create_stores_tags(async_client):
    response = await async_client.post(
        f"{BASE}/preset/prompt",
        json={
            "name": "Prompted",
            "prompt": "Say hello",
            "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
            "tags": ["Software Factory"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["tags"] == ["software-factory"]


async def test_dispatched_draft_keeps_tags(async_client):
    created = await async_client.post(
        f"{BASE}/drafts",
        json={"endpoint": "/v1", "draft": _raw_body("Draft", tags=["qa"])},
    )
    assert created.status_code == 201, created.text

    dispatched = await async_client.post(
        f"{BASE}/drafts/{created.json()['id']}/dispatch"
    )
    assert dispatched.status_code == 201, dispatched.text
    automation_id = dispatched.json()["automation_id"]

    detail = await async_client.get(f"{BASE}/{automation_id}")
    assert detail.json()["tags"] == ["qa"]


# --- List filter ---


async def test_list_filters_by_tag_and_counts_only_matches(async_client):
    await _create(async_client, "One", tags=["software-factory"])
    await _create(async_client, "Two", tags=["qa", "software-factory"])
    await _create(async_client, "Three", tags=["qa"])
    await _create(async_client, "Four")

    response = await async_client.get(BASE, params={"tag": "Software Factory"})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert sorted(a["name"] for a in data["automations"]) == ["One", "Two"]


async def test_list_tag_filter_pages_over_matches(async_client):
    for index in range(3):
        await _create(async_client, f"Tagged {index}", tags=["software-factory"])
    await _create(async_client, "Untagged")

    response = await async_client.get(
        BASE, params={"tag": "software-factory", "limit": 2, "offset": 2}
    )

    data = response.json()
    assert (data["total"], len(data["automations"])) == (3, 1)


async def test_list_rejects_invalid_tag_filter(async_client):
    response = await async_client.get(BASE, params={"tag": "not_a_slug"})
    assert response.status_code == 422, response.text


# --- Tag facets ---


async def test_tags_endpoint_counts_live_org_automations(async_client, async_session):
    async_session.add_all(
        [
            _row("A", ["software-factory", "qa"]),
            _row("B", ["software-factory"]),
            _row("C", ["alpha"]),
            _row("D", []),
            _row("Deleted", ["software-factory", "gone"], deleted_at=utcnow()),
            _row("Other org", ["software-factory"], org_id=OTHER_ORG_ID),
        ]
    )
    await async_session.commit()

    response = await async_client.get(f"{BASE}/tags")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "tags": [
            {"tag": "software-factory", "count": 2},
            {"tag": "alpha", "count": 1},
            {"tag": "qa", "count": 1},
        ]
    }


async def test_tags_endpoint_is_empty_without_tags(async_client):
    await _create(async_client, "Untagged")
    response = await async_client.get(f"{BASE}/tags")
    assert response.json() == {"tags": []}


# --- Edit ---


async def test_patch_replaces_and_clears_tags(async_client):
    created = await _create(async_client, "Editable", tags=["qa"])
    url = f"{BASE}/{created['id']}"

    replaced = await async_client.patch(url, json={"tags": ["Software Factory"]})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["tags"] == ["software-factory"]

    cleared = await async_client.patch(url, json={"tags": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["tags"] == []


async def test_patch_without_tags_keeps_them(async_client):
    created = await _create(async_client, "Kept", tags=["qa"])
    response = await async_client.patch(
        f"{BASE}/{created['id']}", json={"name": "Renamed"}
    )
    assert response.json()["tags"] == ["qa"]


async def test_patch_rejects_null_tags(async_client):
    created = await _create(async_client, "Nullable", tags=["qa"])
    response = await async_client.patch(f"{BASE}/{created['id']}", json={"tags": None})
    assert response.status_code == 422, response.text


async def test_only_the_creator_can_edit_tags(async_client, async_session):
    automation = _row("Teammate's", ["qa"], user_id=OTHER_USER_ID)
    async_session.add(automation)
    await async_session.commit()

    response = await async_client.patch(
        f"{BASE}/{automation.id}", json={"tags": ["software-factory"]}
    )

    assert response.status_code == 403, response.text
    await async_session.refresh(automation)
    assert automation.tags == ["qa"]


# --- Git Sync ---


def _serialized_fields(tags: list[str]) -> dict:
    automation = _row("Synced", tags, id=uuid.uuid4())
    files = serialize_automation(automation, None)
    return yaml.safe_load(files["automation.yaml"].decode())


def test_git_sync_export_omits_empty_tags():
    assert "tags" not in _serialized_fields([])


def test_git_sync_export_writes_tags():
    assert _serialized_fields(["software-factory"])["tags"] == ["software-factory"]


async def test_git_sync_import_round_trips_tags(async_session: AsyncSession):
    files = serialize_automation(
        _row("Synced", ["software-factory", "qa"], id=uuid.uuid4()), None
    )
    deserialized = deserialize_automation(files)
    assert deserialized is not None

    await _create_automation_from_git(
        async_session,
        _Owner(TEST_USER_ID, TEST_ORG_ID),
        "synced",
        deserialized,
        files,
        "test-head",
        [],
    )
    await async_session.flush()

    imported = await async_session.scalar(select(Automation))
    assert imported is not None
    assert imported.tags == ["software-factory", "qa"]
