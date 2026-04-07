"""Tests for the event router endpoint."""

import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient

from automation.auth import AuthenticatedUser
from automation.config import get_settings
from automation.models import Automation


@pytest.fixture
def org_id(mock_authenticated_user: AuthenticatedUser) -> uuid.UUID:
    """Get org_id from authenticated user fixture."""
    return mock_authenticated_user.org_id


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Clear settings cache before and after each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def github_push_payload() -> dict:
    """Sample GitHub push event payload."""
    return {
        "event_type": "push",
        "raw_payload": {
            "ref": "refs/heads/main",
            "before": "abc123",
            "after": "def456",
            "commits": [
                {
                    "id": "def456",
                    "message": "Test commit",
                    "author": {"name": "Test", "email": "test@example.com"},
                }
            ],
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        },
    }


@pytest.fixture
def github_pr_payload() -> dict:
    """Sample GitHub pull_request event payload."""
    return {
        "event_type": "pull_request",
        "raw_payload": {
            "action": "opened",
            "pull_request": {
                "id": 1,
                "number": 42,
                "title": "Test PR",
                "state": "open",
                "draft": False,
                "merged": False,
                "head": {"ref": "feature/test", "sha": "abc123"},
                "base": {"ref": "main", "sha": "def456"},
                "user": {"id": 1, "login": "testuser"},
            },
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        },
    }


def sign_payload(payload: dict, secret: str) -> tuple[str, bytes]:
    """Generate HMAC signature for payload.

    Returns tuple of (signature, body_bytes) since we need to send the exact
    same bytes that were signed.
    """
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}", body


@pytest.mark.asyncio
async def test_receive_github_event_no_matching_automations(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    github_push_payload: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test receiving GitHub event with no matching automations."""
    # Set up the GitHub webhook secret
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    signature, body = sign_payload(github_push_payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["received"] is True
    assert data["matched"] == 0
    assert data["runs_created"] == []


@pytest.mark.asyncio
async def test_receive_github_event_with_matching_automation(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    github_push_payload: dict,
    async_session,
    monkeypatch: pytest.MonkeyPatch,
    mock_authenticated_user,
):
    """Test receiving GitHub event that matches an automation."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    # Create an event-triggered automation
    automation = Automation(
        id=uuid.uuid4(),
        user_id=mock_authenticated_user.user_id,
        org_id=org_id,
        name="Test Push Automation",
        tarball_path="oh-internal://uploads/test.tar.gz",
        entrypoint="python main.py",
        trigger={
            "type": "event",
            "source": "github",
            "on": "push",  # Match push events
        },
    )
    async_session.add(automation)
    await async_session.commit()

    signature, body = sign_payload(github_push_payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["received"] is True
    assert data["matched"] == 1
    assert len(data["runs_created"]) == 1


@pytest.mark.asyncio
async def test_receive_github_event_invalid_signature(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    github_push_payload: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that invalid signature is rejected."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    _, body = sign_payload(github_push_payload, "test-secret")

    # Wrong signature
    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert "Invalid signature" in response.json()["detail"]


@pytest.mark.asyncio
async def test_receive_github_event_missing_signature(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    github_push_payload: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that missing signature is rejected."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    _, body = sign_payload(github_push_payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={"Content-Type": "application/json"},
        # No X-Hub-Signature-256 header
    )

    assert response.status_code == 401
    assert "Missing signature" in response.json()["detail"]


@pytest.mark.asyncio
async def test_receive_github_event_undetectable_payload(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that undetectable payload structure returns 400."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    # Payload with raw_payload that doesn't match any known GitHub event structure
    payload = {"raw_payload": {"data": "test"}}
    signature, body = sign_payload(payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "Cannot detect github event type" in response.json()["detail"]


@pytest.mark.asyncio
async def test_receive_github_event_missing_raw_payload(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that missing raw_payload returns 400."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    # Payload with event_type but no raw_payload
    payload = {"event_type": "push"}
    signature, body = sign_payload(payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "Missing raw_payload" in response.json()["detail"]


@pytest.mark.asyncio
async def test_receive_github_event_malformed_payload(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that malformed payload returns 400."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    # Payload with event_type but invalid raw_payload for that type
    payload = {
        "event_type": "push",
        "raw_payload": {"invalid": "data"},  # Missing required fields
    }
    signature, body = sign_payload(payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "Failed to parse event" in response.json()["detail"]


@pytest.mark.asyncio
async def test_receive_github_event_unknown_event_type(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that unknown GitHub event type returns 400."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    payload = {
        "event_type": "unknown_github_event",
        "raw_payload": {"data": "test"},
    }
    signature, body = sign_payload(payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "Failed to parse event" in response.json()["detail"]


@pytest.mark.asyncio
async def test_receive_github_event_filter_mismatch(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    github_push_payload: dict,
    async_session,
    monkeypatch: pytest.MonkeyPatch,
    mock_authenticated_user,
):
    """Test that events not matching filters don't create runs."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    # Create automation that filters on different repo (using JMESPath filter)
    automation = Automation(
        id=uuid.uuid4(),
        user_id=mock_authenticated_user.user_id,
        org_id=org_id,
        name="Test Push Automation",
        tarball_path="oh-internal://uploads/test.tar.gz",
        entrypoint="python main.py",
        trigger={
            "type": "event",
            "source": "github",
            "on": "push",
            "filter": "repository.full_name == 'different/repo'",
        },
    )
    async_session.add(automation)
    await async_session.commit()

    signature, body = sign_payload(github_push_payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["received"] is True
    assert data["matched"] == 0  # No match due to filter


@pytest.mark.asyncio
async def test_receive_unknown_source(
    async_client: AsyncClient,
    org_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that unknown source without custom webhook returns 404."""
    monkeypatch.setenv("AUTOMATION_GITHUB_APP_WEBHOOK_SECRET", "test-secret")

    payload = {"data": "test"}
    signature, body = sign_payload(payload, "test-secret")

    response = await async_client.post(
        f"/v1/events/{org_id}/unknown-source",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 404
    assert "Unknown webhook source" in response.json()["detail"]
