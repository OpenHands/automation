"""Behavioral tests for the API-key-aware CORS policy.

ApiKeyAwareCORSMiddleware splits incoming requests into two CORS regimes:

  * API-key requests (``Authorization: Bearer …`` or ``X-Session-API-Key``)
    get a permissive policy — ``Access-Control-Allow-Origin: *`` with
    credentials disabled — so API-key clients such as the local
    agent-server GUI can call the service directly from the browser.
  * Cookie / anonymous requests keep the strict origin allowlist with
    credentials enabled, exactly as before the middleware was introduced.

Exercised from a browser's perspective with Starlette's TestClient: a
preflight + a real request from a non-allowlisted origin must be accepted
with an API key and rejected without one.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openhands.automation.app import app
from openhands.automation.middleware import ApiKeyAwareCORSMiddleware


ALLOWED_ORIGIN = "https://app.all-hands.dev"
FOREIGN_ORIGIN = "http://localhost:3001"


def _build_client() -> TestClient:
    """Build a throwaway app so the dispatch matrix is tested in isolation."""
    test_app = FastAPI()

    @test_app.get("/resource")
    def resource():
        return {"ok": True}

    test_app.add_middleware(
        ApiKeyAwareCORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
    )
    return TestClient(test_app)


class TestApiKeyAwareCORSMiddleware:
    """Dispatch-matrix tests for the middleware in isolation."""

    @pytest.mark.parametrize(
        "requested_headers",
        ["authorization, x-org-id", "x-session-api-key"],
    )
    def test_api_key_preflight_from_foreign_origin_gets_wildcard(
        self, requested_headers
    ):
        """Preflights advertising an API-key header get wildcard CORS."""
        # Arrange
        client = _build_client()

        # Act
        response = client.options(
            "/resource",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": requested_headers,
            },
        )

        # Assert — wildcard origin, and credentials MUST be absent: browsers
        # reject `*` combined with allow-credentials.
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in {
            k.lower() for k in response.headers
        }

    @pytest.mark.parametrize(
        "auth_headers",
        [
            {"Authorization": "Bearer test-api-key"},
            {"X-Session-API-Key": "test-session-key"},
        ],
    )
    def test_api_key_request_from_foreign_origin_gets_wildcard(self, auth_headers):
        """Actual (non-preflight) requests carrying an API key get wildcard CORS."""
        # Arrange
        client = _build_client()

        # Act
        response = client.get(
            "/resource",
            headers={"Origin": FOREIGN_ORIGIN, **auth_headers},
        )

        # Assert
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in {
            k.lower() for k in response.headers
        }

    def test_credentialless_preflight_from_foreign_origin_is_blocked(self):
        """Preflights without API-key headers stay on the strict allowlist."""
        # Arrange
        client = _build_client()

        # Act
        response = client.options(
            "/resource",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        # Assert — no allow-origin header means the browser will block.
        assert response.headers.get("access-control-allow-origin") is None

    def test_cookie_request_from_allowed_origin_preserves_strict_cors(self):
        """The pre-existing cookie/session path is unchanged: the specific
        origin is echoed (never wildcard) with credentials enabled."""
        # Arrange
        client = _build_client()

        # Act
        response = client.get("/resource", headers={"Origin": ALLOWED_ORIGIN})

        # Assert
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_preflight_with_lookalike_header_does_not_match_authorization(self):
        """Whole-header-name matching: ``x-my-authorization-token`` must not
        be treated as an API-key request via substring match."""
        # Arrange
        client = _build_client()

        # Act
        response = client.options(
            "/resource",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-my-authorization-token",
            },
        )

        # Assert
        assert response.headers.get("access-control-allow-origin") is None


class TestAppCORSIntegration:
    """The real app must dispatch API-key CORS for the GUI's direct calls."""

    def test_health_preflight_with_authorization_gets_wildcard(self):
        """The preflight a browser sends before the GUI's automation health
        check is answered with wildcard CORS by the real app."""
        # Arrange
        client = TestClient(app)

        # Act
        response = client.options(
            "/api/automation/health",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization, x-org-id",
            },
        )

        # Assert — the strict allowlist used to answer this with 400 and no
        # allow-origin header, forcing the GUI through its local proxy hop.
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"

    def test_health_request_with_bearer_gets_wildcard(self):
        """The bearer-authenticated health request itself carries wildcard
        CORS through the real app's middleware stack."""
        # Arrange
        client = TestClient(app)

        # Act
        response = client.get(
            "/api/automation/health",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Authorization": "Bearer test-api-key",
            },
        )

        # Assert
        assert response.headers.get("access-control-allow-origin") == "*"
