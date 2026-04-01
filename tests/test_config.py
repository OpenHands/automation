"""Tests for configuration module."""

from automation.config import Settings


class TestRootPathExtraction:
    """Verify root_path is correctly extracted from base_url."""

    def test_root_path_with_path_component(self):
        """Extract path from full URL."""
        settings = Settings(base_url="https://app.all-hands.dev/api/automation")
        assert settings.root_path == "/api/automation"

    def test_root_path_with_trailing_slash(self):
        """Trailing slash should be stripped."""
        settings = Settings(base_url="https://app.all-hands.dev/api/automation/")
        assert settings.root_path == "/api/automation"

    def test_root_path_without_path(self):
        """URL at root should return empty string."""
        settings = Settings(base_url="https://app.all-hands.dev")
        assert settings.root_path == ""

    def test_root_path_with_only_slash(self):
        """URL with only root path should return empty string."""
        settings = Settings(base_url="https://app.all-hands.dev/")
        assert settings.root_path == ""

    def test_root_path_empty_base_url(self):
        """Empty base_url should return empty string."""
        settings = Settings(base_url="")
        assert settings.root_path == ""

    def test_root_path_default_base_url(self):
        """Default settings (no base_url) should return empty root_path."""
        settings = Settings()
        assert settings.root_path == ""
