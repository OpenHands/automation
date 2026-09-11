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
    AutomationRunStatus,
    AutomationState,
)
from openhands.automation.storage import get_file_store


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")

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


async def test_create_draft_rejects_unknown_endpoint_fields(async_client):
    response = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {"name": "Draft", "unexpected": "value"},
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "Draft body does not match endpoint schema"
    assert detail["errors"] == [
        {
            "field": "unexpected",
            "code": "extra_forbidden",
            "message": "Extra inputs are not permitted",
        }
    ]


async def test_update_draft_rejects_body_invalid_for_endpoint(async_client):
    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={"endpoint": "/v1/preset/prompt", "draft": {"name": "Draft"}},
    )

    response = await async_client.patch(
        f"/api/automation/v1/drafts/{created.json()['id']}",
        json={"draft": {"name": "Draft", "tarball_path": "oh-internal://uploads/x"}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "Draft body does not match endpoint schema"
    assert detail["errors"][0]["field"] == "tarball_path"
    assert detail["errors"][0]["code"] == "extra_forbidden"


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


async def test_dispatch_event_draft_with_synthetic_payload(
    async_client, async_session, draft_file_store
):
    """An event-triggered draft can be test-dispatched with a user-supplied
    synthetic payload, bypassing webhook signature verification."""
    from openhands.automation.models import CustomWebhook

    webhook = CustomWebhook(
        org_id=TEST_ORG_ID,
        name="Custom",
        source="custom-test",
        webhook_secret="whsec_test",
        event_key_expr="type",
        signature_header="X-Signature-256",
    )
    async_session.add(webhook)
    await async_session.commit()

    synthetic = {"type": "issue.created", "action": "opened"}

    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Event Draft",
                "prompt": "Summarize the issue.",
                "trigger": {
                    "type": "event",
                    "source": "custom-test",
                    "on": "issue.created",
                },
            },
        },
    )
    assert created.status_code == 201
    assert created.json()["dispatchable"] is True

    response = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch",
        json={"event_payload": synthetic},
    )

    assert response.status_code == 201
    run_data = response.json()
    assert run_data["status"] == "PENDING"
    assert run_data["trigger_source"] == "manual"

    run = await async_session.get(AutomationRun, uuid.UUID(run_data["id"]))
    assert run is not None
    assert run.event_payload == synthetic


async def test_dispatch_complete_draft_again_reuses_and_overwrites_materialized_draft(
    async_client, async_session, draft_file_store
):
    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Repeatable draft",
                "prompt": "First prompt.",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            },
        },
    )
    first_dispatch = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch"
    )
    assert first_dispatch.status_code == 201

    draft = await async_session.get(AutomationDraft, uuid.UUID(created.json()["id"]))
    assert draft is not None
    first_automation_id = draft.materialized_automation_id
    assert first_automation_id is not None

    updated = await async_client.patch(
        f"/api/automation/v1/drafts/{created.json()['id']}",
        json={
            "draft": {
                "name": "Repeatable draft",
                "prompt": "Second prompt.",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            }
        },
    )
    assert updated.status_code == 200
    assert updated.json()["dispatchable"] is True

    second_dispatch = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch"
    )
    assert second_dispatch.status_code == 201

    await async_session.refresh(draft)
    assert draft.materialized_automation_id == first_automation_id
    assert draft.last_test_run_id == uuid.UUID(second_dispatch.json()["id"])

    automation = await async_session.get(Automation, first_automation_id)
    assert automation is not None
    assert automation.prompt == "Second prompt."
    assert automation.lifecycle_status == AutomationState.DRAFT
    assert automation.enabled is False
    assert draft_file_store.write_stream.await_count == 2


async def test_edit_materialized_draft_incomplete_keeps_projection_and_dispatch_fails(
    async_client, async_session, draft_file_store
):
    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Completable draft",
                "prompt": "Complete prompt.",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            },
        },
    )
    first_dispatch = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch"
    )
    assert first_dispatch.status_code == 201

    draft = await async_session.get(AutomationDraft, uuid.UUID(created.json()["id"]))
    assert draft is not None
    automation_id = draft.materialized_automation_id
    assert automation_id is not None

    updated = await async_client.patch(
        f"/api/automation/v1/drafts/{created.json()['id']}",
        json={"draft": {"name": "Now incomplete"}},
    )
    assert updated.status_code == 200
    assert updated.json()["dispatchable"] is False

    second_dispatch = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch"
    )
    assert second_dispatch.status_code == 422

    await async_session.refresh(draft)
    assert draft.materialized_automation_id == automation_id
    assert draft.last_test_run_id == uuid.UUID(first_dispatch.json()["id"])

    automation = await async_session.get(Automation, automation_id)
    assert automation is not None
    assert automation.prompt == "Complete prompt."
    assert draft_file_store.write_stream.await_count == 1


async def test_dispatch_draft_does_not_overwrite_linked_non_draft_automation(
    async_client, async_session, draft_file_store
):
    active = Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Finalized automation",
        prompt="Do not overwrite.",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="python main.py",
        enabled=True,
        lifecycle_status=AutomationState.ACTIVE,
    )
    draft = AutomationDraft(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        endpoint="/v1/preset/prompt",
        name="Draft linked to finalized automation",
        draft_body={
            "name": "Draft linked to finalized automation",
            "prompt": "New draft prompt.",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )
    async_session.add_all([active, draft])
    await async_session.flush()
    draft.materialized_automation_id = active.id
    await async_session.commit()

    response = await async_client.post(f"/api/automation/v1/drafts/{draft.id}/dispatch")

    assert response.status_code == 201
    await async_session.refresh(draft)
    await async_session.refresh(active)
    assert draft.materialized_automation_id != active.id
    assert active.prompt == "Do not overwrite."
    assert active.lifecycle_status == AutomationState.ACTIVE
    assert active.deleted_at is None

    new_automation = await async_session.get(
        Automation, draft.materialized_automation_id
    )
    assert new_automation is not None
    assert new_automation.prompt == "New draft prompt."
    assert new_automation.lifecycle_status == AutomationState.DRAFT


async def test_delete_draft_soft_deletes_materialized_draft_automation_and_run(
    async_client, async_session, draft_file_store
):
    created = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Deletable draft",
                "prompt": "Test prompt.",
                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            },
        },
    )
    dispatched = await async_client.post(
        f"/api/automation/v1/drafts/{created.json()['id']}/dispatch"
    )
    assert dispatched.status_code == 201

    draft = await async_session.get(AutomationDraft, uuid.UUID(created.json()["id"]))
    assert draft is not None
    automation = await async_session.get(Automation, draft.materialized_automation_id)
    run = await async_session.get(AutomationRun, uuid.UUID(dispatched.json()["id"]))
    assert automation is not None
    assert run is not None

    response = await async_client.delete(f"/api/automation/v1/drafts/{draft.id}")

    assert response.status_code == 204
    await async_session.refresh(draft)
    await async_session.refresh(automation)
    await async_session.refresh(run)
    assert draft.deleted_at is not None
    assert automation.deleted_at == draft.deleted_at
    assert automation.lifecycle_status == AutomationState.DRAFT
    assert automation.enabled is False
    assert run.status == AutomationRunStatus.SKIPPED
    assert run.completed_at == draft.deleted_at
    assert run.status_detail["detail"] == "Automation draft deleted by user"


async def test_delete_draft_does_not_delete_linked_non_draft_automation(
    async_client, async_session
):
    active = Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="Finalized automation",
        prompt="Keep me.",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="python main.py",
        enabled=True,
        lifecycle_status=AutomationState.ACTIVE,
    )
    draft = AutomationDraft(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        endpoint="/v1/preset/prompt",
        name="Linked to active automation",
        draft_body={"name": "Linked to active automation"},
    )
    async_session.add_all([active, draft])
    await async_session.flush()
    draft.materialized_automation_id = active.id
    await async_session.commit()

    response = await async_client.delete(f"/api/automation/v1/drafts/{draft.id}")

    assert response.status_code == 204
    await async_session.refresh(draft)
    await async_session.refresh(active)
    assert draft.deleted_at is not None
    assert active.deleted_at is None
    assert active.enabled is True
    assert active.lifecycle_status == AutomationState.ACTIVE
