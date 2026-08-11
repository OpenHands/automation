"""Tests for OpenAPI documentation endpoints."""


class TestOpenAPIEndpoints:
    """Tests for OpenAPI spec and documentation UI endpoints."""

    def test_openapi_json_accessible(self, sync_client):
        """GET /api/automation/openapi.json returns the OpenAPI spec."""
        response = sync_client.get("/api/automation/openapi.json")

        assert response.status_code == 200
        data = response.json()
        assert data["info"]["title"] == "OpenHands Automations Service"
        assert "paths" in data

    def test_swagger_ui_accessible(self, sync_client):
        """GET /api/automation/docs returns Swagger UI HTML."""
        response = sync_client.get("/api/automation/docs")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert b"swagger" in response.content.lower()

    def test_redoc_accessible(self, sync_client):
        """GET /api/automation/redoc returns ReDoc HTML."""
        response = sync_client.get("/api/automation/redoc")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert b"redoc" in response.content.lower()
