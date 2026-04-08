"""Tests for configuration module."""

from automation.config import Settings


class TestResolvedBaseUrl:
    """Verify resolved_base_url includes the /api/automation path."""

    def test_resolved_base_url_with_base_url_set(self):
        """When base_url is set, it is returned as-is."""
        settings = Settings(base_url="https://app.all-hands.dev/api/automation")
        assert settings.resolved_base_url == "https://app.all-hands.dev/api/automation"

    def test_resolved_base_url_fallback_includes_base_path(self):
        """When base_url is empty, localhost fallback includes /api/automation."""
        settings = Settings(base_url="", server_port=8000)
        assert settings.resolved_base_url == "http://localhost:8000/api/automation"

    def test_resolved_base_url_fallback_custom_port(self):
        """Localhost fallback uses the configured server_port."""
        settings = Settings(base_url="", server_port=9000)
        assert settings.resolved_base_url == "http://localhost:9000/api/automation"
