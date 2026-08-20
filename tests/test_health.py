"""Tests for health check endpoints."""

import importlib.metadata
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from openhands.automation import __version__
from openhands.automation.app import app


@pytest.fixture
async def health_client():
    """Create an async client for endpoints that do not require a real DB."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


class _ReadyConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, statement):
        pass


class _ReadyEngine:
    def connect(self):
        return _ReadyConnection()


class _UnavailableEngine:
    def connect(self):
        raise Exception("DB connection failed")


class TestHealthEndpoints:
    """Tests for health and readiness endpoints."""

    async def test_health_endpoint(self, health_client):
        """GET /health returns ok status."""
        response = await health_client.get("/api/automation/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_ready_endpoint_success(self, health_client):
        """GET /ready returns ready status when DB is available."""
        original_engine = getattr(app.state, "engine", None)
        app.state.engine = _ReadyEngine()

        try:
            response = await health_client.get("/api/automation/ready")

            assert response.status_code == 200
            assert response.json() == {"status": "ready"}
        finally:
            app.state.engine = original_engine

    async def test_ready_endpoint_db_unavailable(self, health_client):
        """GET /ready returns 503 when DB is unavailable."""
        original_engine = getattr(app.state, "engine", None)

        app.state.engine = _UnavailableEngine()

        try:
            response = await health_client.get("/api/automation/ready")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert "error" in data
        finally:
            app.state.engine = original_engine


class TestSdkVersionEndpoint:
    """Tests for GET /sdk-version — called by setup.sh on every automation run."""

    async def test_returns_installed_sdk_version(self, health_client):
        """GET /sdk-version returns the currently installed openhands-sdk version."""
        response = await health_client.get("/api/automation/sdk-version")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert data["version"] == importlib.metadata.version("openhands-sdk")

    async def test_no_auth_required(self, health_client):
        """GET /sdk-version is accessible without any authentication token."""
        # Use a raw client without the auth headers the fixture normally adds
        response = await health_client.get(
            "/api/automation/sdk-version",
            headers={},
        )
        assert response.status_code == 200

    async def test_returns_503_when_package_not_found(self, health_client):
        """GET /sdk-version returns 503 when openhands-sdk is not installed."""
        with patch(
            "openhands.automation.utils.version.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("openhands-sdk"),
        ):
            response = await health_client.get("/api/automation/sdk-version")

        assert response.status_code == 503
        data = response.json()
        assert "error" in data


class TestServerInfoEndpoint:
    """Tests for GET /server_info — public automation service metadata."""

    async def test_returns_package_and_sdk_versions(self, health_client):
        """GET /server_info returns automation package and SDK versions."""
        response = await health_client.get("/api/automation/server_info")

        assert response.status_code == 200
        assert response.json() == {
            "package_version": __version__,
            "sdk_version": importlib.metadata.version("openhands-sdk"),
        }

    async def test_no_auth_required(self, health_client):
        """GET /server_info is accessible without any authentication token."""
        response = await health_client.get(
            "/api/automation/server_info",
            headers={},
        )
        assert response.status_code == 200

    async def test_returns_503_when_sdk_package_not_found(self, health_client):
        """GET /server_info returns 503 when openhands-sdk is not installed."""
        with patch(
            "openhands.automation.utils.version.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("openhands-sdk"),
        ):
            response = await health_client.get("/api/automation/server_info")

        assert response.status_code == 503
        data = response.json()
        assert "error" in data
