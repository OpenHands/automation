"""Integration tests for the automations REST API."""

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import mock_auth_user


@pytest.mark.asyncio
class TestHealthEndpoints:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readiness(self, client):
        resp = await client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


@pytest.mark.asyncio
class TestCreateAutomation:
    async def test_create_success(self, client):
        resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Weekly PR Summary",
                "triggers": {"cron": {"schedule": "0 9 * * 5"}},
                "sdk_code_tarball_path": "s3://bucket/automations/pr-summary.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Weekly PR Summary"
        assert data["enabled"] is True
        assert data["triggers"]["cron"]["schedule"] == "0 9 * * 5"
        assert (
            data["sdk_code_tarball_path"] == "s3://bucket/automations/pr-summary.tar.gz"
        )
        assert "id" in data
        assert data["user_id"] == "test-user-123"

    async def test_create_invalid_cron(self, client):
        resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Bad Cron",
                "triggers": {"cron": {"schedule": "not a cron"}},
                "sdk_code_tarball_path": "s3://bucket/bad.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        assert resp.status_code == 422

    async def test_create_empty_name(self, client):
        resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "",
                "triggers": {"cron": {"schedule": "0 9 * * 5"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestListAutomations:
    async def test_list_empty(self, client):
        resp = await client.get("/api/v1/automations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["automations"] == []
        assert data["total"] == 0

    async def test_list_after_create(self, client):
        for i in range(2):
            await client.post(
                "/api/v1/automations",
                json={
                    "name": f"Auto {i}",
                    "triggers": {"cron": {"schedule": "0 9 * * *"}},
                    "sdk_code_tarball_path": f"s3://bucket/{i}.tar.gz",
                    "api_key": "sk-oh-testkey",
                },
            )
        resp = await client.get("/api/v1/automations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["automations"]) == 2

    async def test_list_isolation_between_users(self, app_instance, settings):
        """User A should not see User B's automations."""
        # Create as user-a
        mock_auth_user(app_instance, "user-a")
        transport = ASGITransport(app=app_instance)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post(
                "/api/v1/automations",
                json={
                    "name": "A's automation",
                    "triggers": {"cron": {"schedule": "0 9 * * *"}},
                    "sdk_code_tarball_path": "s3://bucket/a.tar.gz",
                    "api_key": "sk-oh-testkey",
                },
            )

        # List as user-b
        mock_auth_user(app_instance, "user-b")
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/automations")

        data = resp.json()
        assert data["total"] == 0


@pytest.mark.asyncio
class TestGetAutomation:
    async def test_get_existing(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Test",
                "triggers": {"cron": {"schedule": "*/5 * * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]
        resp = await client.get(f"/api/v1/automations/{auto_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == auto_id

    async def test_get_nonexistent(self, client):
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/automations/{fake_id}")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestUpdateAutomation:
    async def test_update_name(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Original",
                "triggers": {"cron": {"schedule": "0 9 * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/automations/{auto_id}",
            json={"name": "Updated Name"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    async def test_disable_automation(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "To Disable",
                "triggers": {"cron": {"schedule": "0 9 * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/automations/{auto_id}",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


@pytest.mark.asyncio
class TestDeleteAutomation:
    async def test_delete_existing(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "To Delete",
                "triggers": {"cron": {"schedule": "0 9 * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/v1/automations/{auto_id}")
        assert resp.status_code == 204

        # Verify it's gone
        resp = await client.get(f"/api/v1/automations/{auto_id}")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestTriggerAutomation:
    async def test_manual_trigger_success(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Manual Test",
                "triggers": {"cron": {"schedule": "0 9 * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]

        with patch("automation.router.execute_automation") as mock_exec:
            from automation.executor import ExecutionResult

            mock_exec.return_value = ExecutionResult(
                success=True, conversation_id="conv-123"
            )
            resp = await client.post(f"/api/v1/automations/{auto_id}/trigger")

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "COMPLETED"
        assert data["conversation_id"] == "conv-123"
        assert data["trigger_type"] == "manual"

    async def test_manual_trigger_failure(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Fail Test",
                "triggers": {"cron": {"schedule": "0 9 * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]

        with patch("automation.router.execute_automation") as mock_exec:
            from automation.executor import ExecutionResult

            mock_exec.return_value = ExecutionResult(
                success=False, error="V1 API returned 500"
            )
            resp = await client.post(f"/api/v1/automations/{auto_id}/trigger")

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "FAILED"
        assert "500" in data["error_detail"]


@pytest.mark.asyncio
class TestListRuns:
    async def test_list_runs_after_trigger(self, client):
        create_resp = await client.post(
            "/api/v1/automations",
            json={
                "name": "Run Test",
                "triggers": {"cron": {"schedule": "0 9 * * *"}},
                "sdk_code_tarball_path": "s3://bucket/test.tar.gz",
                "api_key": "sk-oh-testkey",
            },
        )
        auto_id = create_resp.json()["id"]

        with patch("automation.router.execute_automation") as mock_exec:
            from automation.executor import ExecutionResult

            mock_exec.return_value = ExecutionResult(
                success=True, conversation_id="conv-abc"
            )
            await client.post(f"/api/v1/automations/{auto_id}/trigger")

        resp = await client.get(f"/api/v1/automations/{auto_id}/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["runs"][0]["conversation_id"] == "conv-abc"
