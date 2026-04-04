"""Tests for health check endpoints."""

from unittest.mock import AsyncMock

from automation.app import app


class TestHealthEndpoints:
    """Tests for health and readiness endpoints."""

    def test_health_endpoint(self, sync_client):
        """GET /health returns ok status."""
        response = sync_client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_ready_endpoint_success(self, async_client):
        """GET /ready returns ready status when DB is available."""
        # async_client fixture sets up app.state.engine
        response = await async_client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    async def test_ready_endpoint_db_unavailable(self, async_client):
        """GET /ready returns 503 when DB is unavailable."""
        original_engine = app.state.engine

        mock_engine = AsyncMock()
        mock_engine.connect.side_effect = Exception("DB connection failed")
        app.state.engine = mock_engine

        try:
            response = await async_client.get("/ready")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert "database unavailable" in data.get("errors", [])
        finally:
            app.state.engine = original_engine
