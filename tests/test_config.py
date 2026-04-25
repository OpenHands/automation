"""Tests for configuration module."""

import warnings

import pytest

from automation.config import LogSettings, Settings


class TestLogSettings:
    """Tests for LogSettings and effective property computation."""

    def test_effective_log_level_normal(self):
        """Normal log level is returned when debug is False."""
        settings = LogSettings(log_level="WARNING", debug=False)
        assert settings.effective_log_level == "WARNING"

    def test_effective_log_level_debug_override(self):
        """DEBUG override sets effective level to DEBUG."""
        settings = LogSettings(log_level="WARNING", debug=True)
        assert settings.effective_log_level == "DEBUG"

    def test_effective_automation_log_level_fallback(self):
        """Automation log level falls back to log_level when not set."""
        settings = LogSettings(log_level="ERROR", automation_log_level=None)
        assert settings.effective_automation_log_level == "ERROR"

    def test_effective_automation_log_level_explicit(self):
        """Explicit automation log level is used when set."""
        settings = LogSettings(log_level="ERROR", automation_log_level="INFO")
        assert settings.effective_automation_log_level == "INFO"

    def test_effective_automation_log_level_debug_override(self):
        """DEBUG override affects automation log level too."""
        settings = LogSettings(automation_log_level="INFO", debug=True)
        assert settings.effective_automation_log_level == "DEBUG"


class TestDeprecatedConstants:
    """Tests for backward-compatible deprecated constants in constants.py."""

    def test_deprecated_constant_emits_warning(self):
        """Accessing deprecated constants emits DeprecationWarning."""
        # Reset the warned set to ensure we get a warning
        from automation import constants

        constants._warned_constants.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = constants.MAX_RUN_DURATION_SECONDS  # noqa: F841
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()

    def test_deprecated_constant_warns_once(self):
        """Repeated access to same constant only warns once."""
        from automation import constants

        constants._warned_constants.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = constants.SANDBOX_POLL_INTERVAL
            _ = constants.SANDBOX_POLL_INTERVAL
            _ = constants.SANDBOX_POLL_INTERVAL
            # Should only have 1 warning despite 3 accesses
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1

    def test_deprecated_constant_returns_config_value(self):
        """Deprecated constants return values from config."""
        from automation import constants
        from automation.config import get_config

        constants._warned_constants.clear()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert constants.MAX_RUN_DURATION_SECONDS == get_config().sandbox.max_run_duration

    def test_nonexistent_constant_raises_attribute_error(self):
        """Accessing nonexistent constant raises AttributeError."""
        from automation import constants

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = constants.DOES_NOT_EXIST


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
