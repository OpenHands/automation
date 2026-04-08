"""Tests for API router endpoints."""

import uuid

from automation.models import Automation
from automation.utils import utcnow


# Test UUIDs matching mock_authenticated_user fixture
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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
        schedule_errors = [
            e for e in detail if e["loc"] == ["body", "trigger", "schedule"]
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

    async def test_list_automations_only_own(self, async_client, async_session):
        """User cannot see other users' automations."""
        # Create automation for different user
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other User Automation",
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

    async def test_get_automation_wrong_user(self, async_client, async_session):
        """Cannot access other user's automation."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other User Automation",
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

    async def test_update_automation_not_found(self, async_client):
        """PATCH on non-existent automation returns 404."""
        fake_id = uuid.uuid4()

        response = await async_client.patch(
            f"/api/automation/v1/{fake_id}",
            json={"name": "Updated"},
        )

        assert response.status_code == 404

    async def test_update_automation_wrong_user(self, async_client, async_session):
        """Cannot update another user's automation."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other User",
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

        response = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")

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

        response = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")

        assert response.status_code == 404

    async def test_dispatch_automation_wrong_user(self, async_client, async_session):
        """Cannot dispatch another user's automation."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other User Automation",
            trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
            tarball_path="s3://bucket/code.tar.gz",
            entrypoint="uv run script.py",
        )
        async_session.add(automation)
        await async_session.commit()

        response = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")

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

        response = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")

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
        dispatch_resp = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")
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

        from automation.models import AutomationRun, AutomationRunStatus

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
            resp = await async_client.post(f"/api/automation/v1/{automation.id}/dispatch")
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

    async def test_list_runs_wrong_user(self, async_client, async_session):
        """Cannot list runs for another user's automation."""
        automation = Automation(
            user_id=OTHER_USER_ID,
            org_id=OTHER_ORG_ID,
            name="Other User Automation",
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
        from automation.models import AutomationRun, AutomationRunStatus

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
