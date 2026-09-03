"""Tests for server-backed automation drafts."""

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from openhands.automation.app import app
from openhands.automation.models import (
    Automation,
    AutomationDraft,
    AutomationRun,
    AutomationState,
)
from openhands.automation.storage import get_file_store


TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
async def draft_file_store():
    store = MagicMock()
    store._storage = {}

    async def write_stream(
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        content = b""
        async for chunk in stream:
            content += chunk
        store._storage[path] = content
        return len(content)

    store.write_stream = AsyncMock(side_effect=write_stream)
    store.delete = MagicMock(side_effect=lambda path: store._storage.pop(path, None))
    app.dependency_overrides[get_file_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_file_store, None)


async def test_create_incomplete_draft_saves_partial_body(async_client, async_session):
    response = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {"name": "Half-filled draft"},
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["draft"] == {"name": "Half-filled draft"}
    assert data["name"] == "Half-filled draft"
    assert data["dispatchable"] is False
    assert data["validation_errors"]

    draft = await async_session.get(AutomationDraft, uuid.UUID(data["id"]))
    assert draft is not None
    assert draft.materialized_automation_id is None


async def test_raw_draft_with_missing_upload_is_not_dispatchable(
    async_client, async_session
):
    missing_upload = uuid.uuid4()
    response = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1",
            "draft": {
                "name": "Raw draft",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
                "tarball_path": f"oh-internal://uploads/{missing_upload}",
                "entrypoint": "python main.py",
            },
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["dispatchable"] is False
    assert data["validation_errors"] == [
        {
            "field": "tarball_path",
            "code": "tarball_path_404",
            "message": "Upload not found",
        }
    ]

    draft = await async_session.get(AutomationDraft, uuid.UUID(data["id"]))
    assert draft is not None
    assert draft.materialized_automation_id is None


async def test_incomplete_draft_dispatch_returns_validation_errors(
    async_client, async_session
):
    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={"endpoint": "/v1/preset/prompt", "draft": {"name": "Incomplete"}},
    )
    draft_id = created.json()["id"]

    response = await async_client.post(f"/api/automation/v1/drafts/{draft_id}/dispatch")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "Draft is not dispatchable"
    assert detail["errors"]
    draft = await async_session.get(AutomationDraft, uuid.UUID(draft_id))
    assert draft is not None
    assert draft.materialized_automation_id is None


async def test_dispatchable_prompt_draft_materializes_disabled_draft_and_manual_run(
    async_client, async_session, draft_file_store
):
    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Runnable draft",
                "prompt": "Write a short greeting.",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            },
        },
    )
    assert created.status_code == 201
    assert created.json()["dispatchable"] is True

    response = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch",
        headers={"X-OpenHands-Telemetry-Distinct-Id": "ph-draft-test"},
    )

    assert response.status_code == 201
    run_data = response.json()
    assert run_data["status"] == "PENDING"
    assert run_data["trigger_source"] == "manual"

    draft = await async_session.get(AutomationDraft, uuid.UUID(created.json()["id"]))
    assert draft is not None
    assert draft.last_test_run_id == uuid.UUID(run_data["id"])
    assert draft.materialized_automation_id is not None

    automation = await async_session.get(Automation, draft.materialized_automation_id)
    assert automation is not None
    assert automation.enabled is False
    assert automation.lifecycle_status == AutomationState.DRAFT
    assert automation.prompt == "Write a short greeting."
    assert automation.tarball_path.startswith("oh-internal://uploads/")

    run = await async_session.get(AutomationRun, uuid.UUID(run_data["id"]))
    assert run is not None
    assert run.automation_id == automation.id
    assert run.trigger_source == "manual"
    assert draft_file_store.write_stream.await_count == 1
