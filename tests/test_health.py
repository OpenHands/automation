"""Tests for health check endpoints."""

import importlib.metadata
from unittest.mock import AsyncMock, patch

from openhands.automation.app import app


class TestHealthEndpoints:
    """Tests for health and readiness endpoints."""

    def test_health_endpoint(self, sync_client):
        """GET /health returns ok status."""
        response = sync_client.get("/api/automation/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_ready_endpoint_success(self, async_client):
        """GET /ready returns ready status when DB is available."""
        # async_client fixture sets up app.state.engine
        response = await async_client.get("/api/automation/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    async def test_ready_endpoint_db_unavailable(self, async_client):
        """GET /ready returns 503 when DB is unavailable."""
        original_engine = app.state.engine

        mock_engine = AsyncMock()
        mock_engine.connect.side_effect = Exception("DB connection failed")
        app.state.engine = mock_engine

        try:
            response = await async_client.get("/api/automation/ready")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert "error" in data
        finally:
            app.state.engine = original_engine


class TestSdkVersionEndpoint:
    """Tests for GET /sdk-version — called by setup.sh on every automation run."""

    async def test_returns_installed_sdk_version(self, async_client):
        """GET /sdk-version returns the currently installed openhands-sdk version."""
        response = await async_client.get("/api/automation/sdk-version")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert data["version"] == importlib.metadata.version("openhands-sdk")

    async def test_no_auth_required(self, async_client):
        """GET /sdk-version is accessible without any authentication token."""
        # Use a raw client without the auth headers the fixture normally adds
        response = await async_client.get(
            "/api/automation/sdk-version",
            headers={},
        )
        assert response.status_code == 200

    async def test_returns_503_when_package_not_found(self, async_client):
        """GET /sdk-version returns 503 when openhands-sdk is not installed."""
        with patch(
            "openhands.automation.app.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("openhands-sdk"),
        ):
            response = await async_client.get("/api/automation/sdk-version")

        assert response.status_code == 503
        data = response.json()
        assert "error" in data
