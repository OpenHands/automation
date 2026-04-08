"""Tests for configuration module."""

from automation.config import Settings


class TestBasePath:
    """Verify base_path is derived from base_url path + /api/automation."""

    def test_base_path_no_base_url(self):
        settings = Settings(base_url="")
        assert settings.base_path == "/api/automation"

    def test_base_path_domain_only(self):
        settings = Settings(base_url="https://app.all-hands.dev")
        assert settings.base_path == "/api/automation"

    def test_base_path_with_subpath(self):
        settings = Settings(base_url="https://domain/acmecorp")
        assert settings.base_path == "/acmecorp/api/automation"

    def test_base_path_strips_trailing_slash(self):
        settings = Settings(base_url="https://domain/acmecorp/")
        assert settings.base_path == "/acmecorp/api/automation"

    def test_base_path_root_slash_only(self):
        settings = Settings(base_url="https://domain/")
        assert settings.base_path == "/api/automation"


class TestResolvedBaseUrl:
    """Verify resolved_base_url appends /api/automation to base_url."""

    def test_resolved_base_url_appends_base_path(self):
        settings = Settings(base_url="https://app.all-hands.dev")
        assert settings.resolved_base_url == "https://app.all-hands.dev/api/automation"

    def test_resolved_base_url_with_subpath(self):
        settings = Settings(base_url="https://domain/acmecorp")
        assert settings.resolved_base_url == "https://domain/acmecorp/api/automation"

    def test_resolved_base_url_strips_trailing_slash(self):
        settings = Settings(base_url="https://app.all-hands.dev/")
        assert settings.resolved_base_url == "https://app.all-hands.dev/api/automation"

    def test_resolved_base_url_fallback(self):
        settings = Settings(base_url="", server_port=8000)
        assert settings.resolved_base_url == "http://localhost:8000/api/automation"

    def test_resolved_base_url_fallback_custom_port(self):
        settings = Settings(base_url="", server_port=9000)
        assert settings.resolved_base_url == "http://localhost:9000/api/automation"
