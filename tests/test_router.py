"""Tests for API router endpoints."""

import uuid

import pytest

from openhands.automation.models import Automation
from openhands.automation.utils import utcnow


# Test UUIDs matching mock_authenticated_user fixture
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def _automation_for_permission_tests(async_session):
    """Create an automation owned by the test user for permission tests."""

    async def _create():
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Permission Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()
        return automation

    return _create


class TestPermissionEnforcement:
    """Tests for require_permission on mutating endpoints."""

    async def test_update_without_permission_returns_403(
        self, readonly_client, async_session
    ):
        """PATCH returns 403 when user lacks manage_automations permission."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await readonly_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"name": "Updated"},
        )

        assert response.status_code == 403
        assert "manage_automations" in response.json()["detail"]

    async def test_delete_without_permission_returns_403(
        self, readonly_client, async_session
    ):
        """DELETE returns 403 when user lacks manage_automations permission."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await readonly_client.delete(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 403
        assert "manage_automations" in response.json()["detail"]


class TestCreateAutomation:
    """Tests for POST /v1 endpoint."""

    async def test_create_automation_success(self, async_client, async_session):
        """Valid request creates automation and returns 201."""
        payload = {
            "name": "My Test Automation",
            "trigger": {"type": "cron", "schedule": "0 9 * * 5", "timezone": "UTC"},
            "tarball_path": "s3://bucket/path/to/code.tar.gz",
            "setup_script_path": "setup.sh",
            "entrypoint": "uv run script.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Test Automation"
        assert data["trigger"] == {
            "type": "cron",
            "schedule": "0 9 * * 5",
            "timezone": "UTC",
        }
        assert data["tarball_path"] == "s3://bucket/path/to/code.tar.gz"
        assert data["setup_script_path"] == "setup.sh"
        assert data["entrypoint"] == "uv run script.py"
        assert data["enabled"] is True
        assert "id" in data
        assert data["user_id"] == str(TEST_USER_ID)

    async def test_create_automation_defaults_to_active_model_profile(
        self, async_client, mock_authenticated_user
    ):
        """Create stores the active model profile when none is requested."""
        mock_authenticated_user.model_profile_names = frozenset({"active-profile"})
        mock_authenticated_user.active_model_profile_name = "active-profile"

        response = await async_client.post(
            "/api/automation/v1",
            json={
                "name": "My Test Automation",
                "trigger": {"type": "cron", "schedule": "0 9 * * 5"},
                "tarball_path": "s3://bucket/path/to/code.tar.gz",
                "entrypoint": "uv run script.py",
            },
        )

        assert response.status_code == 201
        assert response.json()["model"] == "active-profile"

    async def test_create_automation_without_setup_script(self, async_client):
        """Automation can be created without setup_script_path."""
        payload = {
            "name": "No Setup Script",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/path/to/code.tar.gz",
            "entrypoint": "python main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["setup_script_path"] is None
        assert data["entrypoint"] == "python main.py"

    async def test_create_automation_invalid_cron(self, async_client):
        """Invalid cron expression returns 422."""
        payload = {
            "name": "Bad Cron",
            "trigger": {"type": "cron", "schedule": "invalid-cron", "timezone": "UTC"},
            "tarball_path": "s3://bucket/path/to/code.tar.gz",
            "entrypoint": "uv run script.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422
        detail = response.json()["detail"]
        # Discriminated union includes the tag name ("cron") in the path
        schedule_errors = [
            e for e in detail if e["loc"] == ["body", "trigger", "cron", "schedule"]
        ]
        assert len(schedule_errors) == 1
        assert "Invalid cron expression" in schedule_errors[0]["msg"]

    async def test_create_automation_missing_fields(self, async_client):
        """Missing required fields returns 422."""
        payload = {"name": "Incomplete"}

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_missing_entrypoint(self, async_client):
        """Missing entrypoint returns 422."""
        payload = {
            "name": "No Entrypoint",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/path/to/code.tar.gz",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_invalid_tarball_path(self, async_client):
        """tarball_path without valid scheme returns 422."""
        payload = {
            "name": "Bad Path",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "/local/path/code.tar.gz",
            "entrypoint": "uv run main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422
        assert any(
            "tarball_path" in str(e.get("loc", [])) for e in response.json()["detail"]
        )

    async def test_create_automation_internal_upload_scheme_accepted(
        self, async_client
    ):
        """oh-internal:// scheme is accepted by schema validation."""
        payload = {
            "name": "Internal Upload Test",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "oh-internal://uploads/12345678-1234-1234-1234-123456789abc",
            "entrypoint": "uv run main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        # Should pass schema validation (422 would mean schema rejected it)
        # Will get 404 because the upload doesn't exist, but that's fine -
        # we're testing schema validation, not upload validation
        assert response.status_code == 404

    async def test_create_automation_entrypoint_shell_metachar(self, async_client):
        """entrypoint with shell metacharacters returns 422."""
        payload = {
            "name": "Shell Injection",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "uv run main.py; rm -rf /",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_entrypoint_absolute_path(self, async_client):
        """entrypoint with absolute path returns 422."""
        payload = {
            "name": "Absolute Path",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "/usr/bin/python main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_setup_script_path_traversal(self, async_client):
        """setup_script_path with path traversal returns 422."""
        payload = {
            "name": "Path Traversal",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "setup_script_path": "../../etc/shadow",
            "entrypoint": "uv run main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_setup_script_absolute_path(self, async_client):
        """setup_script_path with absolute path returns 422."""
        payload = {
            "name": "Absolute Setup",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "setup_script_path": "/etc/cron.d/backdoor",
            "entrypoint": "uv run main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_setup_script_shell_metachar(self, async_client):
        """setup_script_path with shell metacharacters returns 422."""
        payload = {
            "name": "Shell Metachar Setup",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "setup_script_path": "setup.sh; rm -rf /",
            "entrypoint": "uv run main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_setup_script_valid(self, async_client):
        """Valid setup_script_path is accepted."""
        payload = {
            "name": "Valid Setup",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "setup_script_path": "scripts/setup.sh",
            "entrypoint": "uv run main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 201
        assert response.json()["setup_script_path"] == "scripts/setup.sh"

    async def test_create_automation_with_timeout(self, async_client):
        """Automation can be created with a valid timeout."""
        payload = {
            "name": "With Timeout",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "python main.py",
            "timeout": 300,
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["timeout"] == 300

    async def test_create_automation_without_timeout(self, async_client):
        """Automation can be created without timeout (uses system default)."""
        payload = {
            "name": "No Timeout",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "python main.py",
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["timeout"] is None

    async def test_create_automation_timeout_zero_rejected(self, async_client):
        """Timeout of zero is rejected."""
        payload = {
            "name": "Zero Timeout",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "python main.py",
            "timeout": 0,
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_timeout_negative_rejected(self, async_client):
        """Negative timeout is rejected."""
        payload = {
            "name": "Negative Timeout",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "python main.py",
            "timeout": -100,
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_timeout_exceeds_max_rejected(self, async_client):
        """Timeout exceeding system maximum is rejected."""
        payload = {
            "name": "Too Long Timeout",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "python main.py",
            "timeout": 601,  # MAX_RUN_DURATION_SECONDS is 600 (10 minutes)
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 422

    async def test_create_automation_timeout_at_max_allowed(self, async_client):
        """Timeout at exactly system maximum is allowed."""
        payload = {
            "name": "Max Timeout",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
            "tarball_path": "s3://bucket/code.tar.gz",
            "entrypoint": "python main.py",
            "timeout": 600,  # MAX_RUN_DURATION_SECONDS is 600 (10 minutes)
        }

        response = await async_client.post("/api/automation/v1", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["timeout"] == 600


class TestListAutomations:
    """Tests for GET /v1 endpoint."""

    async def test_list_automations_empty(self, async_client):
        """No automations returns empty list."""
        response = await async_client.get("/api/automation/v1")

        assert response.status_code == 200
        data = response.json()
        assert data["automations"] == []
        assert data["total"] == 0

    async def test_list_automations_returns_own(self, async_client, async_session):
        """Returns automations for authenticated user."""
        # Create an automation for the test user
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get("/api/automation/v1")

        assert response.status_code == 200
        data = response.json()
        assert len(data["automations"]) == 1
        assert data["total"] == 1
        assert data["automations"][0]["name"] == "Test Automation"

    async def test_list_automations_excludes_deleted(self, async_client, async_session):
        """Soft-deleted automations are not returned."""
        # Create a deleted automation
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Deleted Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
            deleted_at=utcnow(),
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get("/api/automation/v1")

        assert response.status_code == 200
        data = response.json()
        assert data["automations"] == []
        assert data["total"] == 0

    async def test_list_automations_cross_org_returns_empty(
        self, async_client, async_session
    ):
        """Automations from another organization are not visible."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other Org Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get("/api/automation/v1")

        assert response.status_code == 200
        data = response.json()
        assert data["automations"] == []
        assert data["total"] == 0

    async def test_list_automations_pagination(self, async_client, async_session):
        """Pagination parameters work correctly."""
        # Create multiple automations
        for i in range(5):
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name=f"Automation {i}",
                trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/path/to/code.tar.gz",
                entrypoint="uv run script.py",
            )
            async_session.add(automation)
        await async_session.commit()

        response = await async_client.get("/api/automation/v1?limit=2&offset=0")

        assert response.status_code == 200
        data = response.json()
        assert len(data["automations"]) == 2
        assert data["total"] == 5


class TestGetAutomation:
    """Tests for GET /v1/{id} endpoint."""

    async def test_get_automation_success(self, async_client, async_session):
        """Valid ID returns automation."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Automation"
        assert data["id"] == str(automation.id)

    async def test_get_automation_returns_prompt(self, async_client, async_session):
        """GET returns prompt field when set."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Prompt Automation",
            prompt="Summarize PRs daily",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 200
        assert response.json()["prompt"] == "Summarize PRs daily"

    async def test_get_automation_without_prompt_returns_null(
        self, async_client, async_session
    ):
        """GET returns null prompt when not set."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="No Prompt Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 200
        assert response.json()["prompt"] is None

    async def test_get_automation_not_found(self, async_client):
        """Invalid ID returns 404."""
        fake_id = uuid.uuid4()

        response = await async_client.get(f"/api/automation/v1/{fake_id}")

        assert response.status_code == 404
        assert "Automation not found" in response.json()["detail"]

    async def test_get_automation_deleted(self, async_client, async_session):
        """Soft-deleted automation returns 404."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Deleted Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
            deleted_at=utcnow(),
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 404

    async def test_get_automation_cross_org_returns_404(
        self, async_client, async_session
    ):
        """Cannot access automation from another organization."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other Org Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 404


class TestDeleteAutomation:
    """Tests for DELETE /v1/{id} endpoint."""

    async def test_delete_automation_soft_deletes(self, async_client, async_session):
        """DELETE sets enabled=False and deleted_at."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="To Delete",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
            enabled=True,
        )
        async_session.add(automation)
        await async_session.commit()
        automation_id = automation.id

        response = await async_client.delete(f"/api/automation/v1/{automation_id}")

        assert response.status_code == 204

        # Refresh from DB
        await async_session.refresh(automation)
        assert automation.enabled is False
        assert automation.deleted_at is not None

    async def test_delete_automation_not_found(self, async_client):
        """DELETE on non-existent ID returns 404."""
        fake_id = uuid.uuid4()

        response = await async_client.delete(f"/api/automation/v1/{fake_id}")

        assert response.status_code == 404

    async def test_delete_automation_cross_org_returns_404(
        self, async_client, async_session
    ):
        """Cannot delete automation from another organization."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other Org Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.delete(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 404

    async def test_delete_automation_already_deleted(self, async_client, async_session):
        """DELETE on already deleted automation returns 404."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Already Deleted",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
            deleted_at=utcnow(),
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.delete(f"/api/automation/v1/{automation.id}")

        assert response.status_code == 404


class TestUpdateAutomation:
    """Tests for PATCH /v1/{id} endpoint."""

    async def test_update_automation_name(self, async_client, async_session):
        """PATCH updates the automation name."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Original Name",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/path/to/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"name": "Updated Name"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["entrypoint"] == "uv run script.py"

    async def test_update_automation_schedule(self, async_client, async_session):
        """PATCH updates the trigger schedule."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"trigger": {"type": "cron", "schedule": "*/5 * * * *"}},
        )

        assert response.status_code == 200
        assert response.json()["trigger"]["schedule"] == "*/5 * * * *"

    async def test_update_automation_disable(self, async_client, async_session):
        """PATCH can disable an automation."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
            enabled=True,
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"enabled": False},
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is False

    async def test_update_automation_model_profile(self, async_client, async_session):
        """PATCH can update the selected model profile."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            model="original-profile",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"model": "new-profile"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["model"] == "new-profile"
        await async_session.refresh(automation)
        assert automation.model == "new-profile"

    async def test_update_automation_null_model_profile_resets_to_active(
        self, async_client, async_session, mock_authenticated_user
    ):
        """PATCH model=null stores the current active profile name."""
        mock_authenticated_user.model_profile_names = frozenset({"active-profile"})
        mock_authenticated_user.active_model_profile_name = "active-profile"
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            model="original-profile",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"model": None},
        )

        assert response.status_code == 200
        assert response.json()["model"] == "active-profile"
        await async_session.refresh(automation)
        assert automation.model == "active-profile"

    async def test_update_automation_unknown_model_profile_rejected(
        self, async_client, async_session, mock_authenticated_user
    ):
        """PATCH rejects unknown model profiles when auth metadata includes names."""
        mock_authenticated_user.model_profile_names = frozenset({"allowed-profile"})
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"model": "missing-profile"},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "Model profile `missing-profile` not found"

    async def test_update_automation_not_found(self, async_client):
        """PATCH on non-existent automation returns 404."""
        fake_id = uuid.uuid4()

        response = await async_client.patch(
            f"/api/automation/v1/{fake_id}",
            json={"name": "Updated"},
        )

        assert response.status_code == 404

    async def test_update_automation_cross_org_returns_404(
        self, async_client, async_session
    ):
        """Cannot update automation from another organization."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other Org",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"name": "Hacked"},
        )

        assert response.status_code == 404

    async def test_update_automation_prompt(self, async_client, async_session):
        """PATCH updates the automation prompt."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Prompt Update Test",
            prompt="Original prompt",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"prompt": "Updated prompt"},
        )

        assert response.status_code == 200
        assert response.json()["prompt"] == "Updated prompt"

    async def test_update_automation_timeout(self, async_client, async_session):
        """Can update automation timeout."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Update Timeout",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
            timeout=300,
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"timeout": 120},
        )

        assert response.status_code == 200
        assert response.json()["timeout"] == 120

    async def test_update_automation_timeout_invalid(self, async_client, async_session):
        """Cannot update timeout to invalid value."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Invalid Timeout Update",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"timeout": -10},
        )

        assert response.status_code == 422

    async def test_update_automation_timeout_exceeds_max(
        self, async_client, async_session
    ):
        """Cannot update timeout to exceed system maximum."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Max Timeout Update",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.patch(
            f"/api/automation/v1/{automation.id}",
            json={"timeout": 700},  # MAX_RUN_DURATION_SECONDS is 600 (10 minutes)
        )

        assert response.status_code == 422


class TestDispatchAutomation:
    """Tests for POST /v1/{id}/dispatch endpoint."""

    async def test_dispatch_automation_success(self, async_client, async_session):
        """Dispatching an automation creates a PENDING run."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Dispatch",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.post(
            f"/api/automation/v1/{automation.id}/dispatch"
        )

        assert response.status_code == 201
        data = response.json()
        assert data["automation_id"] == str(automation.id)
        assert data["status"] == "PENDING"
        assert data["error_detail"] is None
        assert "id" in data
        assert "created_at" in data
        assert data["started_at"] is None
        assert data["completed_at"] is None

    async def test_dispatch_automation_not_found(self, async_client):
        """Dispatching a nonexistent automation returns 404."""
        fake_id = uuid.uuid4()

        response = await async_client.post(f"/api/automation/v1/{fake_id}/dispatch")

        assert response.status_code == 404
        assert "Automation not found" in response.json()["detail"]

    async def test_dispatch_automation_deleted(self, async_client, async_session):
        """Dispatching a soft-deleted automation returns 404."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Deleted Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
            deleted_at=utcnow(),
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.post(
            f"/api/automation/v1/{automation.id}/dispatch"
        )

        assert response.status_code == 404

    async def test_dispatch_automation_cross_org_returns_404(
        self, async_client, async_session
    ):
        """Cannot dispatch automation from another organization."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other Org Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.post(
            f"/api/automation/v1/{automation.id}/dispatch"
        )

        assert response.status_code == 404

    async def test_dispatch_automation_multiple_runs(self, async_client, async_session):
        """Multiple dispatches create multiple independent runs."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Multiple Runs",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        resp1 = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")
        resp2 = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")

        assert resp1.status_code == 201
        assert resp2.status_code == 201

        run1 = resp1.json()
        run2 = resp2.json()

        # Each dispatch creates a unique run
        assert run1["id"] != run2["id"]
        assert run1["automation_id"] == run2["automation_id"] == str(automation.id)
        assert run1["status"] == run2["status"] == "PENDING"

    async def test_dispatch_updates_last_triggered_at(
        self, async_client, async_session
    ):
        """Dispatching updates the automation's last_triggered_at."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Trigger Update",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        assert automation.last_triggered_at is None

        response = await async_client.post(
            f"/api/automation/v1/{automation.id}/dispatch"
        )

        assert response.status_code == 201

        # Refresh from DB to verify last_triggered_at was updated
        await async_session.refresh(automation)
        assert automation.last_triggered_at is not None


class TestListAutomationRuns:
    """Tests for GET /v1/{id}/runs endpoint."""

    async def test_list_runs_empty(self, async_client, async_session):
        """Listing runs for automation with no runs returns empty list."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["runs"] == []
        assert data["total"] == 0

    async def test_list_runs_returns_runs(self, async_client, async_session):
        """Listing runs after dispatch shows created runs."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Dispatch a run
        dispatch_resp = await async_client.post(
            f"/api/automation/v1/{automation.id}/dispatch"
        )
        assert dispatch_resp.status_code == 201
        run_id = dispatch_resp.json()["id"]

        # List runs
        response = await async_client.get(f"/api/automation/v1/{automation.id}/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["runs"]) == 1
        assert data["runs"][0]["id"] == run_id
        assert data["runs"][0]["automation_id"] == str(automation.id)

    async def test_list_runs_ordered_by_latest(self, async_client, async_session):
        """Runs are returned in descending order by creation time."""
        from datetime import timedelta

        from openhands.automation.models import AutomationRun, AutomationRunStatus

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Create runs with explicit different timestamps to ensure ordering
        now = utcnow()
        run_ids = []
        for i in range(3):
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
                created_at=now + timedelta(seconds=i),  # Each run 1 second later
            )
            async_session.add(run)
            await async_session.flush()
            run_ids.append(str(run.id))
        await async_session.commit()

        # List runs
        response = await async_client.get(f"/api/automation/v1/{automation.id}/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3

        # Verify order: latest first (reverse of creation order)
        returned_ids = [r["id"] for r in data["runs"]]
        assert returned_ids == list(reversed(run_ids))

    async def test_list_runs_pagination(self, async_client, async_session):
        """Pagination works correctly."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Dispatch 5 runs
        for _ in range(5):
            resp = await async_client.post(
                f"/api/automation/v1/{automation.id}/dispatch"
            )
            assert resp.status_code == 201

        # Get first page
        response = await async_client.get(
            f"/api/automation/v1/{automation.id}/runs",
            params={"limit": 2, "offset": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert len(data["runs"]) == 2

        # Get second page
        response = await async_client.get(
            f"/api/automation/v1/{automation.id}/runs",
            params={"limit": 2, "offset": 2},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert len(data["runs"]) == 2

    async def test_list_runs_not_found(self, async_client):
        """Listing runs for nonexistent automation returns 404."""
        fake_id = uuid.uuid4()

        response = await async_client.get(f"/api/automation/v1/{fake_id}/runs")

        assert response.status_code == 404
        assert "Automation not found" in response.json()["detail"]

    async def test_list_runs_cross_org_returns_404(self, async_client, async_session):
        """Cannot list runs for automation from another organization."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other Org Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/runs")

        assert response.status_code == 404

    async def test_list_runs_deleted_automation(self, async_client, async_session):
        """Listing runs for deleted automation returns 404."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Deleted Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
            deleted_at=utcnow(),
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/runs")

        assert response.status_code == 404

    async def test_list_runs_limit_exceeds_max(self, async_client, async_session):
        """Requesting more than 100 results returns 422."""
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(
            f"/api/automation/v1/{automation.id}/runs",
            params={"limit": 101},
        )

        assert response.status_code == 422

    async def test_list_runs_default_limit_is_50(self, async_client, async_session):
        """Default limit is 50 results."""
        from openhands.automation.models import AutomationRun, AutomationRunStatus

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Create 60 runs directly in DB
        for _ in range(60):
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.PENDING,
            )
            async_session.add(run)
        await async_session.commit()

        # List runs without specifying limit
        response = await async_client.get(f"/api/automation/v1/{automation.id}/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 60
        assert len(data["runs"]) == 50  # Default limit


class TestBackwardCompatibility:
    """Tests ensuring existing cron triggers work with discriminated union."""

    async def test_cron_trigger_create_and_retrieve(self, async_client):
        """Existing cron triggers should still work with discriminated union."""
        # Create automation with cron trigger using S3 path (valid scheme)
        response = await async_client.post(
            "/api/automation/v1",
            json={
                "name": "Cron Backward Compat Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *", "timezone": "UTC"},
                "tarball_path": "s3://bucket/backward-compat-test.tar.gz",
                "entrypoint": "python main.py",
            },
        )
        assert response.status_code == 201
        automation_id = response.json()["id"]

        # Verify it can be retrieved with correct trigger type
        response = await async_client.get(f"/api/automation/v1/{automation_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["trigger"]["type"] == "cron"
        assert data["trigger"]["schedule"] == "0 0 * * *"

    async def test_cron_trigger_with_s3_path(self, async_client):
        """Cron trigger with S3 path creates and retrieves correctly."""
        response = await async_client.post(
            "/api/automation/v1",
            json={
                "name": "Cron S3 Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *", "timezone": "UTC"},
                "tarball_path": "s3://bucket/code.tar.gz",
                "entrypoint": "python main.py",
            },
        )
        assert response.status_code == 201
        automation_id = response.json()["id"]

        # Verify it can be retrieved
        response = await async_client.get(f"/api/automation/v1/{automation_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["trigger"]["type"] == "cron"
        assert data["trigger"]["schedule"] == "0 0 * * *"

    async def test_trigger_missing_type_returns_422(self, async_client):
        """Trigger without type field returns 422 (fail fast)."""
        response = await async_client.post(
            "/api/automation/v1",
            json={
                "name": "Missing Type",
                "trigger": {"schedule": "0 0 * * *"},  # No "type" field
                "tarball_path": "s3://bucket/code.tar.gz",
                "entrypoint": "python main.py",
            },
        )
        assert response.status_code == 422


class TestCompleteRun:
    """Tests for POST /runs/{run_id}/complete endpoint."""

    async def test_complete_run_saves_conversation_id_for_completed_runs(
        self, async_client, async_session
    ):
        """Complete endpoint saves conversation_id when status is COMPLETED."""
        from openhands.automation.models import AutomationRun, AutomationRunStatus

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Create a RUNNING run
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
        )
        async_session.add(run)
        await async_session.commit()

        # Complete it as COMPLETED with a conversation_id
        response = await async_client.post(
            f"/api/automation/v1/runs/{run.id}/complete",
            json={"status": "COMPLETED", "conversation_id": "conv-completed-123"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "COMPLETED"
        assert data["conversation_id"] == "conv-completed-123"

        # Verify in database
        await async_session.refresh(run)
        assert run.conversation_id == "conv-completed-123"
        assert run.status == AutomationRunStatus.COMPLETED

    async def test_complete_run_saves_conversation_id_for_failed_runs(
        self, async_client, async_session
    ):
        """Complete endpoint saves conversation_id when status is FAILED."""
        from openhands.automation.models import AutomationRun, AutomationRunStatus

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Create a RUNNING run
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
        )
        async_session.add(run)
        await async_session.commit()

        # Complete it as FAILED with a conversation_id and error
        response = await async_client.post(
            f"/api/automation/v1/runs/{run.id}/complete",
            json={
                "status": "FAILED",
                "conversation_id": "conv-failed-456",
                "error": "Test error message",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "FAILED"
        assert data["conversation_id"] == "conv-failed-456"
        assert data["error_detail"] == "Test error message"

        # Verify in database
        await async_session.refresh(run)
        assert run.conversation_id == "conv-failed-456"
        assert run.status == AutomationRunStatus.FAILED
        assert run.error_detail == "Test error message"

    async def test_complete_run_without_conversation_id(
        self, async_client, async_session
    ):
        """Complete endpoint works without conversation_id."""
        from openhands.automation.models import AutomationRun, AutomationRunStatus

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Create a RUNNING run
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
        )
        async_session.add(run)
        await async_session.commit()

        # Complete without conversation_id
        response = await async_client.post(
            f"/api/automation/v1/runs/{run.id}/complete",
            json={"status": "COMPLETED"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "COMPLETED"
        assert data["conversation_id"] is None

    async def test_complete_run_not_running_returns_409(
        self, async_client, async_session
    ):
        """Complete endpoint returns 409 if run is not in RUNNING state."""
        from openhands.automation.models import AutomationRun, AutomationRunStatus

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Test Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        # Create a PENDING run (not RUNNING)
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.PENDING,
        )
        async_session.add(run)
        await async_session.commit()

        # Try to complete it
        response = await async_client.post(
            f"/api/automation/v1/runs/{run.id}/complete",
            json={"status": "COMPLETED"},
        )

        assert response.status_code == 409
        assert "PENDING" in response.json()["detail"]


class TestDownloadTarball:
    """Tests for GET /{automation_id}/tarball endpoint."""

    async def test_internal_url_returns_tarball_bytes(
        self, async_client, async_session
    ):
        """Tarball bytes are returned for a completed internal upload."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.models import TarballUpload, UploadStatus
        from openhands.automation.storage import get_file_store

        upload = TarballUpload(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="test-tarball",
            status=UploadStatus.COMPLETED,
            storage_path=f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/upload.tar",
        )
        async_session.add(upload)
        await async_session.commit()

        tarball_bytes = b"fake tarball content"
        mock_store = MagicMock()
        mock_store.read.return_value = tarball_bytes
        app.dependency_overrides[get_file_store] = lambda: mock_store

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="My Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path=f"oh-internal://uploads/{upload.id}",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/tarball")

        assert response.status_code == 200
        assert response.content == tarball_bytes
        assert response.headers["content-type"] == "application/x-tar"
        assert 'filename="My Automation.tar"' in response.headers["content-disposition"]
        mock_store.read.assert_called_once_with(upload.storage_path)

    async def test_internal_url_upload_deleted_returns_404(
        self, async_client, async_session
    ):
        """404 is returned when the referenced internal upload has been soft-deleted."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.models import TarballUpload, UploadStatus
        from openhands.automation.storage import get_file_store

        upload = TarballUpload(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="deleted-upload",
            status=UploadStatus.COMPLETED,
            storage_path="uploads/test/deleted.tar",
            deleted_at=utcnow(),
        )
        async_session.add(upload)
        await async_session.commit()

        app.dependency_overrides[get_file_store] = lambda: MagicMock()

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="My Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path=f"oh-internal://uploads/{upload.id}",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/tarball")

        assert response.status_code == 404
        assert "deleted" in response.json()["detail"].lower()

    async def test_internal_url_file_missing_from_storage_returns_404(
        self, async_client, async_session
    ):
        """404 is returned when the file is missing from the storage backend."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.models import TarballUpload, UploadStatus
        from openhands.automation.storage import get_file_store

        upload = TarballUpload(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="missing-file",
            status=UploadStatus.COMPLETED,
            storage_path="uploads/test/missing.tar",
        )
        async_session.add(upload)
        await async_session.commit()

        mock_store = MagicMock()
        mock_store.read.side_effect = FileNotFoundError("file not found")
        app.dependency_overrides[get_file_store] = lambda: mock_store

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="My Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path=f"oh-internal://uploads/{upload.id}",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/tarball")

        assert response.status_code == 404
        assert "not found in storage" in response.json()["detail"]

    async def test_https_url_returns_redirect(self, async_client, async_session):
        """302 redirect is returned for an https:// tarball URL."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: MagicMock()

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="External Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="https://example.com/tarball.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(
            f"/api/automation/v1/{automation.id}/tarball",
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["location"] == "https://example.com/tarball.tar.gz"

    async def test_s3_url_returns_422(self, async_client, async_session):
        """422 is returned for an s3:// tarball URL that cannot be proxied."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: MagicMock()

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="S3 Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://my-bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/tarball")

        assert response.status_code == 422
        assert "s3" in response.json()["detail"]

    async def test_automation_not_found_returns_404(self, async_client):
        """404 is returned for an unknown automation ID."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: MagicMock()

        response = await async_client.get(f"/api/automation/v1/{uuid.uuid4()}/tarball")

        assert response.status_code == 404

    async def test_http_url_returns_redirect(self, async_client, async_session):
        """302 redirect is returned for an http:// tarball URL."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: MagicMock()

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="HTTP External Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="http://example.com/tarball.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(
            f"/api/automation/v1/{automation.id}/tarball",
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["location"] == "http://example.com/tarball.tar.gz"

    async def test_internal_url_sanitizes_filename(self, async_client, async_session):
        """Control characters and path separators in automation name are stripped."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.models import TarballUpload, UploadStatus
        from openhands.automation.storage import get_file_store

        upload = TarballUpload(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="test-tarball",
            status=UploadStatus.COMPLETED,
            storage_path=f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/upload.tar",
        )
        async_session.add(upload)
        await async_session.commit()

        mock_store = MagicMock()
        mock_store.read.return_value = b"fake tarball"
        app.dependency_overrides[get_file_store] = lambda: mock_store

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name='My"Auto/mation\nName',
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path=f"oh-internal://uploads/{upload.id}",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/tarball")

        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        # Extract just the filename value (between the wrapper quotes)
        filename = disposition.split('filename="')[1].rstrip('"')
        assert "\n" not in filename
        assert "\r" not in filename
        assert '"' not in filename
        assert "/" not in filename

    async def test_internal_url_storage_error_returns_500(
        self, async_client, async_session
    ):
        """500 is returned when the storage backend raises an unexpected error."""
        from unittest.mock import MagicMock

        from openhands.automation.app import app
        from openhands.automation.models import TarballUpload, UploadStatus
        from openhands.automation.storage import get_file_store

        upload = TarballUpload(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="error-upload",
            status=UploadStatus.COMPLETED,
            storage_path="uploads/test/error.tar",
        )
        async_session.add(upload)
        await async_session.commit()

        mock_store = MagicMock()
        mock_store.read.side_effect = OSError("disk full")
        app.dependency_overrides[get_file_store] = lambda: mock_store

        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="My Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path=f"oh-internal://uploads/{upload.id}",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.get(f"/api/automation/v1/{automation.id}/tarball")

        assert response.status_code == 500
        assert "retrieve" in response.json()["detail"].lower()
