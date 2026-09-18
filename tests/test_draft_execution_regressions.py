"""Draft execution regressions that use SQLite instead of Docker."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from openhands.automation.app import app
from openhands.automation.db import set_sqlite_mode, using_sqlite
from openhands.automation.models import Automation, AutomationDraft, AutomationRun, Base
from openhands.automation.storage import get_file_store
from openhands.automation.storage.local import LocalFileStore


OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
async def async_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Reuse the shared API fixtures with a self-contained SQLite database."""
    previous_sqlite_mode = using_sqlite()
    set_sqlite_mode(True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/drafts.db")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        set_sqlite_mode(previous_sqlite_mode)


@pytest.fixture(autouse=True)
def draft_storage(tmp_path: Path):
    store = LocalFileStore(str(tmp_path / "storage"))
    app.dependency_overrides[get_file_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_file_store, None)


def _raw_body(version: str = "first") -> dict:
    return {
        "name": "Draft execution regression",
        "entrypoint": "python main.py",
        "tarball_path": f"https://example.com/{version}.tar.gz",
        "trigger": {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
    }


async def _create_draft(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/automation/v1/drafts",
        json={"endpoint": "/v1", "draft": _raw_body()},
    )
    assert response.status_code == 201, response.text
    assert response.json()["dispatchable"] is True, response.text
    return response.json()["id"]


async def _dispatch(client: httpx.AsyncClient, draft_id: str) -> dict:
    response = await client.post(f"/api/automation/v1/drafts/{draft_id}/dispatch")
    assert response.status_code == 201, response.text
    return response.json()


async def test_cron_draft_rejects_synthetic_event_payload(async_client, async_session):
    draft_id = await _create_draft(async_client)

    response = await async_client.post(
        f"/api/automation/v1/drafts/{draft_id}/dispatch",
        json={"event_payload": {"type": "synthetic"}},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "event_payload can only be used with event-triggered drafts"
    )

    runs = (await async_session.execute(select(AutomationRun))).scalars().all()
    assert runs == []


@pytest.mark.parametrize("operation", ["dispatch", "activate"])
async def test_normal_api_cannot_bypass_current_draft_validation(
    async_client, operation
):
    draft_id = await _create_draft(async_client)
    first_run = await _dispatch(async_client, draft_id)
    changed = await async_client.patch(
        f"/api/automation/v1/drafts/{draft_id}",
        json={"draft": {"name": "Now incomplete"}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["dispatchable"] is False
    rejected = await async_client.post(f"/api/automation/v1/drafts/{draft_id}/dispatch")
    assert rejected.status_code == 422, rejected.text

    automation_id = first_run["automation_id"]
    if operation == "dispatch":
        response = await async_client.post(
            f"/api/automation/v1/{automation_id}/dispatch"
        )
    else:
        response = await async_client.patch(
            f"/api/automation/v1/{automation_id}", json={"state": "ACTIVE"}
        )
    assert response.status_code == 422, (
        f"The ordinary {operation} API accepted stale executable configuration "
        f"although the current draft is incomplete: {response.status_code}"
    )
    assert response.json()["detail"] == rejected.json()["detail"]


async def test_only_draft_creator_can_mutate_or_dispatch_materialized_draft(
    async_client, async_session, mock_authenticated_user
):
    draft_id = await _create_draft(async_client)
    first_run = await _dispatch(async_client, draft_id)
    automation_id = first_run["automation_id"]

    draft = await async_session.get(AutomationDraft, uuid.UUID(draft_id))
    automation = await async_session.get(Automation, uuid.UUID(automation_id))
    assert draft is not None
    assert automation is not None
    assert draft.user_id == mock_authenticated_user.user_id
    assert automation.user_id == mock_authenticated_user.user_id

    mock_authenticated_user.user_id = OTHER_USER_ID

    edited = await async_client.patch(
        f"/api/automation/v1/drafts/{draft_id}",
        json={"draft": _raw_body("second")},
    )
    assert edited.status_code == 403, edited.text

    dispatched = await async_client.post(
        f"/api/automation/v1/drafts/{draft_id}/dispatch"
    )
    assert dispatched.status_code == 403, dispatched.text

    deleted = await async_client.delete(f"/api/automation/v1/drafts/{draft_id}")
    assert deleted.status_code == 403, deleted.text

    await async_session.refresh(draft)
    await async_session.refresh(automation)
    assert draft.deleted_at is None
    assert draft.draft_body == _raw_body()
    assert automation.tarball_path == _raw_body()["tarball_path"]
    assert automation.user_id != mock_authenticated_user.user_id

    runs = (
        (
            await async_session.execute(
                select(AutomationRun).where(
                    AutomationRun.automation_id == automation.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [run.id for run in runs] == [uuid.UUID(first_run["id"])]
