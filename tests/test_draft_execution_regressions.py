"""Draft execution regressions that use SQLite instead of Docker."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from openhands.automation.app import app
from openhands.automation.db import set_sqlite_mode, using_sqlite
from openhands.automation.models import Base
from openhands.automation.storage import get_file_store
from openhands.automation.storage.local import LocalFileStore


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
