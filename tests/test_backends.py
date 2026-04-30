"""Tests for execution backends."""

import pytest

from automation.backends import (
    CloudSandboxBackend,
    ExecutionContext,
    LocalAgentServerBackend,
    get_backend,
)


class TestExecutionContext:
    """Tests for ExecutionContext dataclass."""

    def test_basic_fields(self):
        """ExecutionContext stores agent_url and session_key."""
        ctx = ExecutionContext(
            agent_url="http://localhost:3000",
            session_key="test-key",
        )
        assert ctx.agent_url == "http://localhost:3000"
        assert ctx.session_key == "test-key"
        assert ctx.sandbox_id is None

    def test_cloud_mode_fields(self):
        """ExecutionContext can store Cloud-specific fields."""
        ctx = ExecutionContext(
            agent_url="https://sandbox.example.com",
            session_key="session-key",
            sandbox_id="sandbox-123",
            api_url="https://api.example.com",
            api_key="api-key",
        )
        assert ctx.sandbox_id == "sandbox-123"
        assert ctx.api_url == "https://api.example.com"
        assert ctx.api_key == "api-key"


class TestLocalAgentServerBackend:
    """Tests for LocalAgentServerBackend."""

    def test_is_local_mode(self):
        """LocalAgentServerBackend reports local mode."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="test-key",
        )
        assert backend.is_local_mode is True

    def test_strips_trailing_slash(self):
        """URL trailing slash is stripped."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000/",
            api_key="test-key",
        )
        assert backend.agent_server_url == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_acquire_returns_context(self):
        """acquire() returns ExecutionContext with configured values."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
        )
        # acquire() doesn't actually make HTTP calls in local mode
        ctx = await backend.acquire(None)  # type: ignore
        assert ctx.agent_url == "http://localhost:3000"
        assert ctx.session_key == "local-key"
        assert ctx.sandbox_id is None

    @pytest.mark.asyncio
    async def test_release_is_noop(self):
        """release() is a no-op for local backend."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
        )
        ctx = ExecutionContext(
            agent_url="http://localhost:3000",
            session_key="local-key",
        )
        # Should not raise
        await backend.release(None, ctx)  # type: ignore


class TestCloudSandboxBackend:
    """Tests for CloudSandboxBackend."""

    def test_is_local_mode(self):
        """CloudSandboxBackend reports cloud mode."""
        backend = CloudSandboxBackend(
            api_url="https://app.all-hands.dev",
            api_key="sk-test",
        )
        assert backend.is_local_mode is False

    def test_strips_trailing_slash(self):
        """URL trailing slash is stripped."""
        backend = CloudSandboxBackend(
            api_url="https://app.all-hands.dev/",
            api_key="sk-test",
        )
        assert backend.api_url == "https://app.all-hands.dev"

    def test_find_agent_server_url_found(self):
        """_find_agent_server_url extracts agent URL from sandbox response."""
        sandbox = {
            "exposed_urls": [
                {"name": "OTHER", "url": "http://other.example.com"},
                {"name": "AGENT_SERVER", "url": "http://agent.example.com/"},
            ],
            "session_api_key": "session-key",
        }
        result = CloudSandboxBackend._find_agent_server_url(sandbox)
        assert result == ("http://agent.example.com", "session-key")

    def test_find_agent_server_url_not_found(self):
        """_find_agent_server_url returns None if no AGENT_SERVER URL."""
        sandbox = {
            "exposed_urls": [
                {"name": "OTHER", "url": "http://other.example.com"},
            ],
        }
        result = CloudSandboxBackend._find_agent_server_url(sandbox)
        assert result is None

    def test_find_agent_server_url_empty(self):
        """_find_agent_server_url handles empty exposed_urls."""
        sandbox = {"exposed_urls": None}
        result = CloudSandboxBackend._find_agent_server_url(sandbox)
        assert result is None


class TestGetBackend:
    """Tests for get_backend factory function."""

    def test_local_mode(self, monkeypatch):
        """get_backend returns LocalAgentServerBackend when configured."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "local-key")

        # Clear config cache to pick up new env vars
        from automation.config import clear_config_cache

        clear_config_cache()

        backend = get_backend()
        assert isinstance(backend, LocalAgentServerBackend)
        assert backend.agent_server_url == "http://localhost:3000"
        assert backend.api_key == "local-key"

    def test_cloud_mode(self, monkeypatch):
        """get_backend returns CloudSandboxBackend when not in local mode."""
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
        monkeypatch.setenv(
            "AUTOMATION_OPENHANDS_API_BASE_URL", "https://app.all-hands.dev"
        )

        # Clear config cache
        from automation.config import clear_config_cache

        clear_config_cache()

        backend = get_backend(api_key="sk-test")
        assert isinstance(backend, CloudSandboxBackend)
        assert backend.api_url == "https://app.all-hands.dev"
        assert backend.api_key == "sk-test"

    def test_cloud_mode_requires_api_key(self, monkeypatch):
        """get_backend raises ValueError in Cloud mode without api_key."""
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)

        # Clear config cache
        from automation.config import clear_config_cache

        clear_config_cache()

        with pytest.raises(ValueError, match="api_key is required"):
            get_backend()

    def test_local_mode_ignores_api_key(self, monkeypatch):
        """get_backend uses config key in local mode, ignores passed api_key."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "config-key")

        from automation.config import clear_config_cache

        clear_config_cache()

        # Passing an api_key should be ignored in local mode
        backend = get_backend(api_key="ignored-key")
        assert isinstance(backend, LocalAgentServerBackend)
        assert backend.api_key == "config-key"
