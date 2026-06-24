"""Tests for configuration module."""

import warnings

import pytest

from openhands.automation.config import (
    HttpSettings,
    LogSettings,
    SandboxSettings,
    Settings,
    clear_config_cache,
    get_config,
    get_log_settings,
    get_settings,
    get_storage_settings,
)


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
        from openhands.automation import constants

        constants._warned_constants.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = constants.MAX_RUN_DURATION_SECONDS  # noqa: F841
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()
            assert "default_run_duration" in str(w[0].message)

    def test_deprecated_constant_warns_once(self):
        """Repeated access to same constant only warns once."""
        from openhands.automation import constants

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
        from openhands.automation import constants
        from openhands.automation.config import get_config

        constants._warned_constants.clear()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert (
                constants.MAX_RUN_DURATION_SECONDS
                == get_config().sandbox.default_run_duration
            )

    def test_nonexistent_constant_raises_attribute_error(self):
        """Accessing nonexistent constant raises AttributeError."""
        from openhands.automation import constants

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


class TestHttpSettings:
    """Tests for HttpSettings configuration."""

    def test_default_values(self):
        """Default values are reasonable."""
        settings = HttpSettings()
        assert settings.http_timeout == 10.0
        assert settings.http_long_timeout == 60.0
        assert settings.auth_cache_ttl == 20.0
        assert settings.auth_cache_size == 1024
        assert settings.auth_max_retries == 3
        assert settings.auth_initial_backoff == 1.0
        assert settings.auth_max_backoff == 10.0

    def test_custom_values(self):
        """Custom values are accepted."""
        settings = HttpSettings(
            http_timeout=5.0,
            auth_cache_ttl=30.0,
            auth_cache_size=512,
        )
        assert settings.http_timeout == 5.0
        assert settings.auth_cache_ttl == 30.0
        assert settings.auth_cache_size == 512


class TestSandboxSettings:
    """Tests for SandboxSettings configuration."""

    def test_default_values(self):
        """Default values are reasonable."""
        settings = SandboxSettings()
        assert settings.default_run_duration == 600
        assert settings.max_run_duration == 1800
        assert settings.sandbox_poll_interval == 5
        assert settings.sandbox_ready_timeout == 300
        assert settings.rate_limit_min_wait == 10
        assert settings.rate_limit_max_wait == 60
        assert settings.rate_limit_max_retries == 5

    def test_custom_values(self):
        """Custom values are accepted."""
        settings = SandboxSettings(
            default_run_duration=1200,
            max_run_duration=2400,
            sandbox_poll_interval=10,
        )
        assert settings.default_run_duration == 1200
        assert settings.max_run_duration == 2400
        assert settings.sandbox_poll_interval == 10


class TestAppConfig:
    """Tests for the composed AppConfig class."""

    def test_get_config_returns_same_instance(self):
        """get_config() returns cached singleton."""
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2

    def test_clear_config_cache_creates_new_instance(self):
        """clear_config_cache() forces new instance."""
        config1 = get_config()
        clear_config_cache()
        config2 = get_config()
        assert config1 is not config2

    def test_config_sections_accessible(self, monkeypatch):
        """All config sections are accessible."""
        # Storage requires GCS_BUCKET_NAME when FILE_STORE=gcs (default)
        monkeypatch.setenv("GCS_BUCKET_NAME", "test-bucket")
        clear_config_cache()

        config = get_config()
        assert config.service is not None
        assert config.storage is not None
        assert config.log is not None
        assert config.http is not None
        assert config.sandbox is not None


class TestAuthCacheReset:
    """Tests for auth cache reset when config is cleared."""

    def test_auth_cache_reset_on_clear_config(self):
        """Auth cache is reset when clear_config_cache is called."""
        from openhands.automation.auth import _get_auth_cache

        # Ensure cache exists
        cache1 = _get_auth_cache()
        assert cache1 is not None

        # Clear config cache (should also reset auth cache)
        clear_config_cache()

        # Import again to get fresh reference
        from openhands.automation.auth import _auth_cache as auth_cache_after

        # The module-level variable should be None after reset
        assert auth_cache_after is None

        # Getting cache again should create a new one
        cache2 = _get_auth_cache()
        assert cache2 is not None
        # They should be different objects
        assert cache1 is not cache2


class TestLocalModeSettings:
    """Tests for local agent-server mode configuration."""

    def test_is_local_mode_false_by_default(self):
        """is_local_mode is False when agent_server_url is not set."""
        settings = Settings()
        assert settings.is_local_mode is False

    def test_is_local_mode_false_when_empty_string(self):
        """is_local_mode is False when agent_server_url is empty string."""
        settings = Settings(agent_server_url="")
        assert settings.is_local_mode is False

    def test_is_local_mode_true_when_set(self):
        """is_local_mode is True when agent_server_url is configured."""
        settings = Settings(agent_server_url="http://localhost:3000")
        assert settings.is_local_mode is True

    def test_agent_server_url_default(self):
        """agent_server_url defaults to empty string."""
        settings = Settings()
        assert settings.agent_server_url == ""

    def test_agent_server_api_key_default(self):
        """agent_server_api_key defaults to empty string."""
        settings = Settings()
        assert settings.agent_server_api_key == ""

    def test_workspace_base_default(self):
        """workspace_base defaults to /workspace."""
        settings = Settings()
        assert settings.workspace_base == "/workspace"

    def test_db_url_default(self):
        """db_url defaults to empty string."""
        settings = Settings()
        assert settings.db_url == ""

    def test_db_ssl_mode_default(self):
        """db_ssl_mode defaults to empty string."""
        settings = Settings()
        assert settings.db_ssl_mode == ""

    def test_db_ssl_mode_falls_back_to_db_ssl_mode(self, monkeypatch):
        """db_ssl_mode falls back to standard DB_SSL_MODE."""
        monkeypatch.setenv("DB_SSL_MODE", "require")

        settings = Settings()

        assert settings.db_ssl_mode == "require"

    def test_db_ssl_mode_falls_back_to_pgsslmode(self, monkeypatch):
        """db_ssl_mode falls back to standard PGSSLMODE."""
        monkeypatch.setenv("PGSSLMODE", "disable")

        settings = Settings()

        assert settings.db_ssl_mode == "disable"

    def test_automation_db_ssl_mode_takes_precedence(self, monkeypatch):
        """AUTOMATION_DB_SSL_MODE takes precedence over standard fallbacks."""
        monkeypatch.setenv("AUTOMATION_DB_SSL_MODE", "require")
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("PGSSLMODE", "prefer")

        settings = Settings()

        assert settings.db_ssl_mode == "require"

    def test_db_ssl_mode_takes_precedence_over_pgsslmode(self, monkeypatch):
        """DB_SSL_MODE takes precedence over PGSSLMODE."""
        monkeypatch.setenv("DB_SSL_MODE", "require")
        monkeypatch.setenv("PGSSLMODE", "disable")

        settings = Settings()

        assert settings.db_ssl_mode == "require"

    def test_local_mode_full_configuration(self):
        """All local mode settings can be configured together."""
        settings = Settings(
            agent_server_url="http://localhost:3000",
            agent_server_api_key="local-key",
            workspace_base="/my/workspace",
            db_url="sqlite+aiosqlite:////data/automations.db",
        )
        assert settings.is_local_mode is True
        assert settings.agent_server_url == "http://localhost:3000"
        assert settings.agent_server_api_key == "local-key"
        assert settings.workspace_base == "/my/workspace"
        assert settings.db_url == "sqlite+aiosqlite:////data/automations.db"

    def test_local_mode_from_env(self, monkeypatch):
        """Local mode settings are loaded from environment variables."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "env-key")
        monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", "/env/workspace")
        monkeypatch.setenv("AUTOMATION_DB_URL", "sqlite+aiosqlite:////data/test.db")
        monkeypatch.setenv("AUTOMATION_DB_SSL_MODE", "require")
        clear_config_cache()

        settings = Settings()
        assert settings.is_local_mode is True
        assert settings.agent_server_url == "http://localhost:3000"
        assert settings.agent_server_api_key == "env-key"
        assert settings.workspace_base == "/env/workspace"
        assert settings.db_url == "sqlite+aiosqlite:////data/test.db"
        assert settings.db_ssl_mode == "require"


class TestDeprecatedFunctionWarnings:
    """Tests for deprecation warnings on legacy functions."""

    def test_get_settings_emits_warning(self):
        """get_settings() emits DeprecationWarning."""
        from openhands.automation import config

        config._warned_functions.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = get_settings()
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1
            assert "get_settings()" in str(deprecation_warnings[0].message)

    def test_get_storage_settings_emits_warning(self, monkeypatch):
        """get_storage_settings() emits DeprecationWarning."""
        from openhands.automation import config

        # Storage requires GCS_BUCKET_NAME when FILE_STORE=gcs (default)
        monkeypatch.setenv("GCS_BUCKET_NAME", "test-bucket")
        clear_config_cache()
        config._warned_functions.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = get_storage_settings()
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1
            assert "get_storage_settings()" in str(deprecation_warnings[0].message)

    def test_get_log_settings_emits_warning(self):
        """get_log_settings() emits DeprecationWarning."""
        from openhands.automation import config

        config._warned_functions.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = get_log_settings()
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1
            assert "get_log_settings()" in str(deprecation_warnings[0].message)

    def test_deprecated_function_warns_once(self):
        """Repeated calls to deprecated function only warn once."""
        from openhands.automation import config

        config._warned_functions.clear()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = get_settings()
            _ = get_settings()
            _ = get_settings()
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1
