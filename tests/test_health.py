"""Tests for health check endpoints."""

import importlib.metadata
import logging
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from openhands.automation import __version__
from openhands.automation.app import app


@pytest.fixture
async def health_client(monkeypatch):
    """Create an async client for endpoints that do not require a real DB."""
    monkeypatch.setattr(app.state, "readiness_failure", None, raising=False)
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


class _ReadyEngine:
    def connect(self):
        return _ReadyConnection()


class _UnavailableEngine:
    def __init__(
        self,
        error_type: type[Exception] = Exception,
        message: str = "DB connection failed",
    ):
        self.error_type = error_type
        self.message = message

    def connect(self):
        raise self.error_type(self.message)


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

    async def test_ready_endpoint_deduplicates_tracebacks(
        self, health_client, monkeypatch, caplog
    ):
        monkeypatch.setattr(app.state, "engine", _UnavailableEngine(), raising=False)

        with caplog.at_level(logging.ERROR, logger="automation.app"):
            for path in ("/ready", "/api/automation/ready", "/ready"):
                response = await health_client.get(path)
                assert response.status_code == 503
                assert response.json() == {
                    "status": "not_ready",
                    "error": "database unavailable",
                }

        errors = [
            record
            for record in caplog.records
            if record.name == "automation.app" and record.levelno == logging.ERROR
        ]
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert errors[0].exc_info[2] is not None

    async def test_ready_endpoint_logs_failure_changes_and_recovery(
        self, health_client, monkeypatch, caplog
    ):
        monkeypatch.setattr(app.state, "engine", _UnavailableEngine(), raising=False)
        with caplog.at_level(logging.INFO, logger="automation.app"):
            failures = (
                (Exception, "DB connection failed"),
                (Exception, "DB connection timed out"),
                (TimeoutError, "DB connection timed out"),
            )
            for error_type, message in failures:
                app.state.engine = _UnavailableEngine(error_type, message)
                response = await health_client.get("/ready")
                assert response.status_code == 503

            app.state.engine = _ReadyEngine()
            for path in ("/ready", "/api/automation/ready"):
                response = await health_client.get(path)
                assert response.status_code == 200
                assert response.json() == {"status": "ready"}

            app.state.engine = _UnavailableEngine(*failures[-1])
            response = await health_client.get("/api/automation/ready")
            assert response.status_code == 503

        records = [
            record for record in caplog.records if record.name == "automation.app"
        ]
        assert [record.levelno for record in records] == [
            logging.ERROR,
            logging.ERROR,
            logging.ERROR,
            logging.INFO,
            logging.ERROR,
        ]
        assert records[3].getMessage() == "Readiness check recovered"
        for record, (error_type, message) in zip(
            (*records[:3], records[4]), (*failures, failures[-1]), strict=True
        ):
            assert record.exc_info is not None
            assert record.exc_info[0] is error_type
            assert record.exc_info[2] is not None
            assert record.getMessage() == f"Readiness check failed: {message}"


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
