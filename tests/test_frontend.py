"""Tests for frontend static file hosting and frontend_path config."""

import os

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient

from automation.config import Settings


# ---------------------------------------------------------------------------
# Settings.frontend_path
# ---------------------------------------------------------------------------


class TestFrontendPath:
    """Verify frontend_path is derived from base_url, mirroring base_path."""

    def test_no_base_url(self):
        settings = Settings(base_url="")
        assert settings.frontend_path == "/automations"

    def test_domain_only(self):
        settings = Settings(base_url="https://app.all-hands.dev")
        assert settings.frontend_path == "/automations"

    def test_with_subpath(self):
        settings = Settings(base_url="https://domain/acmecorp")
        assert settings.frontend_path == "/acmecorp/automations"

    def test_strips_trailing_slash(self):
        settings = Settings(base_url="https://domain/acmecorp/")
        assert settings.frontend_path == "/acmecorp/automations"

    def test_root_slash_only(self):
        settings = Settings(base_url="https://domain/")
        assert settings.frontend_path == "/automations"

    def test_consistent_with_base_path(self):
        """frontend_path and base_path share the same prefix logic."""
        for url in ["", "https://domain", "https://domain/org"]:
            s = Settings(base_url=url)
            # Both should start with the same prefix (everything before the
            # final path segment).
            fp_prefix = s.frontend_path.rsplit("/automations", 1)[0]
            bp_prefix = s.base_path.rsplit("/api/automation", 1)[0]
            assert fp_prefix == bp_prefix, f"Mismatch for base_url={url!r}"


# ---------------------------------------------------------------------------
# _SPAStaticFiles serving behaviour
# ---------------------------------------------------------------------------

# Reproduce the same class from app.py so we can test it in isolation
# without importing the module-level app (which requires DB env vars etc.)


def _build_test_app(frontend_dir: str, mount_path: str = "/automations") -> FastAPI:
    """Build a minimal FastAPI app with the SPA static files mount."""
    from pathlib import Path

    test_app = FastAPI()
    frontend_path = Path(frontend_dir)
    index_full_path = str(frontend_path / "index.html")
    index_stat = os.stat(index_full_path)

    class SPAStaticFiles(StaticFiles):
        def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
            full_path, stat_result = super().lookup_path(path)
            if stat_result is None:
                return index_full_path, index_stat
            return full_path, stat_result

        def file_response(self, full_path, stat_result, scope, status_code=200):
            response = super().file_response(full_path, stat_result, scope, status_code)
            if "/assets/" in str(full_path):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            else:
                response.headers.setdefault(
                    "Cache-Control", "no-cache, must-revalidate"
                )
            return response

    test_app.mount(
        mount_path,
        SPAStaticFiles(directory=frontend_path, html=True),
        name="frontend",
    )
    return test_app


@pytest.fixture()
def frontend_dir(tmp_path):
    """Create a minimal frontend build directory."""
    (tmp_path / "index.html").write_text("<html><body>SPA</body></html>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app-abc123.js").write_text("console.log('hi')")
    (assets / "style-def456.css").write_text("body{}")
    (tmp_path / "locales" / "en").mkdir(parents=True)
    (tmp_path / "locales" / "en" / "translation.json").write_text("{}")
    return tmp_path


@pytest.fixture()
def spa_client(frontend_dir):
    """AsyncClient wired to a test app with the SPA mount."""
    test_app = _build_test_app(str(frontend_dir))
    return AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test")


class TestSPAStaticFiles:
    """Integration tests for the SPA static file mount."""

    async def test_index_html_served_at_root(self, spa_client):
        r = await spa_client.get("/automations/")
        assert r.status_code == 200
        assert "SPA" in r.text

    async def test_index_cache_control(self, spa_client):
        r = await spa_client.get("/automations/")
        assert r.headers["cache-control"] == "no-cache, must-revalidate"

    async def test_js_asset_served(self, spa_client):
        r = await spa_client.get("/automations/assets/app-abc123.js")
        assert r.status_code == 200
        assert "console" in r.text

    async def test_asset_cache_immutable(self, spa_client):
        r = await spa_client.get("/automations/assets/app-abc123.js")
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    async def test_css_asset_cache_immutable(self, spa_client):
        r = await spa_client.get("/automations/assets/style-def456.css")
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    async def test_spa_fallback_returns_index(self, spa_client):
        """Unknown paths should return index.html for client-side routing."""
        r = await spa_client.get("/automations/some-automation-id")
        assert r.status_code == 200
        assert "SPA" in r.text

    async def test_spa_fallback_cache_control(self, spa_client):
        r = await spa_client.get("/automations/some-automation-id")
        assert r.headers["cache-control"] == "no-cache, must-revalidate"

    async def test_nested_spa_route_fallback(self, spa_client):
        r = await spa_client.get("/automations/automations/abc/runs/123")
        assert r.status_code == 200
        assert "SPA" in r.text

    async def test_non_asset_static_file(self, spa_client):
        """Real non-asset files (e.g. locales) should be served directly."""
        r = await spa_client.get("/automations/locales/en/translation.json")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache, must-revalidate"

    async def test_outside_mount_returns_404(self, spa_client):
        """Paths outside /automations should not be handled."""
        r = await spa_client.get("/other-path")
        assert r.status_code == 404


class TestSPAWithSubpath:
    """Verify the mount works with a non-default prefix (like base_url subpath)."""

    async def test_subpath_mount(self, frontend_dir):
        test_app = _build_test_app(
            str(frontend_dir), mount_path="/acmecorp/automations"
        )
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            r = await client.get("/acmecorp/automations/")
            assert r.status_code == 200
            assert "SPA" in r.text

            r = await client.get("/acmecorp/automations/assets/app-abc123.js")
            assert r.status_code == 200
            assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

            r = await client.get("/acmecorp/automations/any-route")
            assert r.status_code == 200
            assert "SPA" in r.text
