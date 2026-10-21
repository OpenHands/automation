"""Tests for execution backends."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openhands.automation.backends import (
    CloudSandboxBackend,
    ExecutionContext,
    LocalAgentServerBackend,
    get_backend,
)
from openhands.automation.backends.cloud import (
    AgentProfileSeedError,
    _concurrency_limit_detail,
    summarize_status_detail,
)
from openhands.automation.backends.local import (
    local_runs_root,
    resolve_local_workspace_base,
)
from openhands.automation.exceptions import (
    ConcurrencyLimitReachedError,
    SandboxProvisioningError,
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

    @pytest.fixture
    def mock_run(self):
        """Create a mock AutomationRun."""
        run = MagicMock()
        run.id = "test-run-123"
        run.sandbox_id = None
        # Default to None — individual tests override when needed
        run.bash_command_id = None
        return run

    def test_is_local_mode(self, mock_run):
        """LocalAgentServerBackend reports local mode."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="test-key",
            run=mock_run,
        )
        assert backend.is_local_mode is True

    def test_strips_trailing_slash(self, mock_run):
        """URL trailing slash is stripped."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000/",
            api_key="test-key",
            run=mock_run,
        )
        assert backend.agent_server_url == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_get_execution_context_returns_context(self, mock_run):
        """get_execution_context() returns ExecutionContext with configured values."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
            run=mock_run,
        )
        # get_execution_context() doesn't make HTTP calls in local mode
        ctx = await backend.get_execution_context(None)  # type: ignore
        assert ctx.agent_url == "http://localhost:3000"
        assert ctx.session_key == "local-key"
        assert ctx.sandbox_id is None

    @pytest.mark.asyncio
    async def test_release_context_is_noop(self, mock_run):
        """release_context() is a no-op for local backend."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
            run=mock_run,
        )
        ctx = ExecutionContext(
            agent_url="http://localhost:3000",
            session_key="local-key",
        )
        # Should not raise
        await backend.release_context(None, ctx)  # type: ignore

    @pytest.mark.asyncio
    async def test_get_api_key_returns_config_key(self, mock_run):
        """get_api_key() returns the pre-configured API key."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
            run=mock_run,
        )
        api_key = await backend.get_api_key()
        assert api_key == "local-key"

    def test_build_env_vars(self, mock_run):
        """build_env_vars() returns required env vars for local mode."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="agent-server-key",
            run=mock_run,
            callback_api_key="automation-service-key",
        )
        env_vars = backend.build_env_vars()
        # WORKSPACE_BASE should be run-isolated (includes run_id)
        assert env_vars["AGENT_SERVER_URL"] == "http://localhost:3000"
        assert env_vars["SESSION_API_KEY"] == "agent-server-key"
        # Workspace should be isolated per-run and have ~ expanded
        assert "test-run-123" in env_vars["WORKSPACE_BASE"]
        assert Path(env_vars["WORKSPACE_BASE"]).parts[-2:] == (
            "automation-runs",
            "test-run-123",
        )
        assert "~" not in env_vars["WORKSPACE_BASE"]  # ~ should be expanded
        # Callback API key should be the automation service's key (NOT agent server key)
        assert env_vars["AUTOMATION_CALLBACK_API_KEY"] == "automation-service-key"

    def test_build_env_vars_custom_workspace_base(self, mock_run):
        """build_env_vars() uses custom workspace_base when provided."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="agent-key",
            run=mock_run,
            workspace_base="/custom/workspace",
            callback_api_key="callback-key",
        )
        env_vars = backend.build_env_vars()
        # Custom workspace_base is used as the base, but still isolated per-run
        assert env_vars["AGENT_SERVER_URL"] == "http://localhost:3000"
        assert env_vars["SESSION_API_KEY"] == "agent-key"
        assert (
            Path(env_vars["WORKSPACE_BASE"])
            == Path("/custom/workspace") / "automation-runs" / "test-run-123"
        )
        assert env_vars["AUTOMATION_CALLBACK_API_KEY"] == "callback-key"

    def test_build_env_vars_no_callback_key(self, mock_run):
        """build_env_vars() omits callback key when callback_api_key is not set."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="agent-key",
            run=mock_run,
            # No callback_api_key provided
        )
        env_vars = backend.build_env_vars()
        assert env_vars["AGENT_SERVER_URL"] == "http://localhost:3000"
        assert env_vars["SESSION_API_KEY"] == "agent-key"
        # No callback key when callback_api_key is not set
        assert "AUTOMATION_CALLBACK_API_KEY" not in env_vars

    def test_build_env_vars_sandbox_url_override(self, mock_run):
        """sandbox_agent_server_url overrides AGENT_SERVER_URL only in the
        sandbox export — the backend itself still uses agent_server_url."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:18000",
            api_key="agent-key",
            run=mock_run,
            sandbox_agent_server_url="http://127.0.0.1:8000",
        )
        env_vars = backend.build_env_vars()
        # In-sandbox bash chain sees the override
        assert env_vars["AGENT_SERVER_URL"] == "http://127.0.0.1:8000"
        # But the backend still uses the original URL for its own HTTP calls
        assert backend.agent_server_url == "http://localhost:18000"

    def test_build_env_vars_sandbox_url_falls_back(self, mock_run):
        """When sandbox_agent_server_url is None or empty, the in-sandbox
        AGENT_SERVER_URL falls back to agent_server_url (current behaviour)."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="agent-key",
            run=mock_run,
            sandbox_agent_server_url=None,
        )
        assert backend.sandbox_agent_server_url is None
        env_vars = backend.build_env_vars()
        assert env_vars["AGENT_SERVER_URL"] == "http://localhost:3000"

    def test_get_work_dir_default_workspace(self, mock_run):
        """get_work_dir() returns isolated directory with ~ expanded."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="test-key",
            run=mock_run,
        )
        work_dir = backend.get_work_dir("my-run-id")
        # Should expand ~ and include run_id in isolation path
        assert Path(work_dir).parts[-2:] == ("automation-runs", "my-run-id")
        assert "~" not in work_dir  # ~ should be expanded

    def test_get_work_dir_custom_workspace(self, mock_run):
        """get_work_dir() uses custom workspace_base when provided."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="test-key",
            run=mock_run,
            workspace_base="/my/custom/base",
        )
        work_dir = backend.get_work_dir("run-456")
        assert Path(work_dir) == Path("/my/custom/base") / "automation-runs" / "run-456"

    def test_workspace_base_resolution_contract(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        explicit = tmp_path / "explicit"
        cases = (
            (None, tmp_path / ".openhands" / "workspaces"),
            ("", tmp_path / ".openhands" / "workspaces"),
            (str(explicit), explicit),
            ("~", tmp_path),
        )

        for configured, expected in cases:
            assert Path(resolve_local_workspace_base(configured)) == expected
            assert local_runs_root(configured) == expected / "automation-runs"
            backend = LocalAgentServerBackend(
                agent_server_url="http://localhost:3000",
                api_key="test-key",
                run=mock_run,
                workspace_base=configured,
            )
            assert Path(backend.get_work_dir("run-id")) == (
                expected / "automation-runs" / "run-id"
            )

    @pytest.mark.asyncio
    async def test_verify_run_calls_agent_server(self, mock_run):
        """verify_run() delegates to verify_run_on_agent_server and
        forwards the stored bash_command_id so the verifier filters
        BashOutput events by *this run's* command instead of sampling
        the most recent BashOutput on a shared agent server.
        """
        mock_run.bash_command_id = "abc123def456"
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
            run=mock_run,
        )
        mock_result = MagicMock(verified=True, exit_code=0)

        with patch(
            "openhands.automation.backends.local.verify_run_on_agent_server",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_verify:
            result = await backend.verify_run("run-123")
            assert result == mock_result
            mock_verify.assert_called_once_with(
                agent_url="http://localhost:3000",
                session_key="local-key",
                run_id="run-123",
                bash_command_id="abc123def456",
            )

    @pytest.mark.asyncio
    async def test_cleanup_after_verification_is_noop(self, mock_run):
        """cleanup_after_verification() is a no-op for local backend."""
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000",
            api_key="local-key",
            run=mock_run,
        )
        # Should not raise
        await backend.cleanup_after_verification("run-123")


class TestCloudSandboxBackend:
    """Tests for CloudSandboxBackend."""

    @pytest.fixture
    def mock_run(self):
        """Create a mock AutomationRun."""
        run = MagicMock()
        run.sandbox_id = "sandbox-123"
        # Default to None — individual tests override when needed
        run.bash_command_id = None
        return run

    def test_is_local_mode(self, mock_run):
        """CloudSandboxBackend reports cloud mode."""
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)
        assert backend.is_local_mode is False

    def test_strips_trailing_slash(self, mock_run):
        """URL trailing slash is stripped."""
        backend = CloudSandboxBackend(
            api_url="https://app.all-hands.dev/", run=mock_run
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

    @pytest.mark.asyncio
    async def test_get_api_key_mints_per_user_key(self, mock_run):
        """get_api_key() mints a per-user key via service key."""
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)

        with patch(
            "openhands.automation.backends.cloud.get_api_key_for_automation_run",
            new_callable=AsyncMock,
            return_value="sk-user-minted",
        ) as mock_mint:
            api_key = await backend.get_api_key()
            assert api_key == "sk-user-minted"
            mock_mint.assert_called_once_with(mock_run)

    @pytest.mark.asyncio
    async def test_build_env_vars(self, mock_run):
        """build_env_vars() includes Cloud API credentials after key is minted."""
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)

        with patch(
            "openhands.automation.backends.cloud.get_api_key_for_automation_run",
            new_callable=AsyncMock,
            return_value="sk-user",
        ):
            # First ensure API key is minted
            await backend.get_api_key()

            env_vars = backend.build_env_vars()
            assert env_vars == {
                "OPENHANDS_API_KEY": "sk-user",
                "OPENHANDS_CLOUD_API_URL": "https://app.all-hands.dev",
            }

    def test_build_env_vars_raises_without_api_key(self, mock_run):
        """build_env_vars() raises if API key not initialized."""
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)
        with pytest.raises(RuntimeError, match="API key not initialized"):
            backend.build_env_vars()

    @pytest.mark.asyncio
    async def test_verify_run_without_sandbox_id(self, mock_run):
        """verify_run() returns error when sandbox_id is missing."""
        mock_run.sandbox_id = None
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)

        result = await backend.verify_run("run-123")
        assert result.verified is False
        assert result.error is not None and "No sandbox_id" in result.error

    @pytest.mark.asyncio
    async def test_verify_run_calls_verify_run_status(self, mock_run):
        """verify_run() delegates to verify_run_status and forwards the
        stored bash_command_id so BashOutput lookups are scoped to this
        run's specific command.
        """
        mock_run.bash_command_id = "deadbeefcafebabe"
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)
        mock_result = MagicMock(verified=True, exit_code=0)

        with (
            patch(
                "openhands.automation.backends.cloud.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="sk-user",
            ),
            patch(
                "openhands.automation.backends.cloud.verify_run_status",
                new_callable=AsyncMock,
                return_value=mock_result,
            ) as mock_verify,
        ):
            result = await backend.verify_run("run-123")
            assert result == mock_result
            mock_verify.assert_called_once_with(
                api_url="https://app.all-hands.dev",
                api_key="sk-user",
                sandbox_id="sandbox-123",
                run_id="run-123",
                bash_command_id="deadbeefcafebabe",
            )

    @pytest.mark.asyncio
    async def test_cleanup_after_verification_deletes_sandbox(self, mock_run):
        """cleanup_after_verification() deletes sandbox when called."""
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)

        with (
            patch(
                "openhands.automation.backends.cloud.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="sk-user",
            ),
            patch(
                "openhands.automation.backends.cloud.cleanup_sandbox",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            await backend.cleanup_after_verification("run-123")
            mock_cleanup.assert_called_once_with(
                api_url="https://app.all-hands.dev",
                api_key="sk-user",
                sandbox_id="sandbox-123",
                run_id="run-123",
            )


class TestGetBackend:
    """Tests for get_backend factory function."""

    @pytest.fixture
    def mock_run(self):
        """Create a mock AutomationRun."""
        run = MagicMock()
        run.sandbox_id = "sandbox-123"
        return run

    def test_local_mode(self, monkeypatch, mock_run):
        """get_backend returns LocalAgentServerBackend when configured."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "local-key")

        # Clear config cache to pick up new env vars
        from openhands.automation.config import clear_config_cache

        clear_config_cache()

        backend = get_backend(mock_run)
        assert isinstance(backend, LocalAgentServerBackend)
        assert backend.agent_server_url == "http://localhost:3000"
        assert backend.api_key == "local-key"

    def test_cloud_mode(self, monkeypatch, mock_run):
        """get_backend returns CloudSandboxBackend when not in local mode."""
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
        monkeypatch.setenv(
            "AUTOMATION_OPENHANDS_API_BASE_URL", "https://app.all-hands.dev"
        )

        # Clear config cache
        from openhands.automation.config import clear_config_cache

        clear_config_cache()

        backend = get_backend(mock_run)
        assert isinstance(backend, CloudSandboxBackend)
        assert backend.api_url == "https://app.all-hands.dev"


class TestConcurrencyLimitDetection:
    """Tests for `_concurrency_limit_detail`, the discriminator that tells an
    organization concurrency-limit 429 (→ mark run SKIPPED) apart from a
    transient rate-limit 429 (→ retry as before)."""

    @staticmethod
    def _resp(status: int, *, json=None, raw: bytes | None = None) -> httpx.Response:
        req = httpx.Request("POST", "https://app.all-hands.dev/api/v1/sandboxes")
        if raw is not None:
            return httpx.Response(status, request=req, content=raw)
        return httpx.Response(status, request=req, json=json)

    def test_detects_nested_fastapi_detail(self):
        """The real shape: FastAPI nests the HTTPException detail under "detail"."""
        resp = self._resp(
            429,
            json={
                "detail": {
                    "error": "CONCURRENCY_LIMIT_REACHED",
                    "message": "You have reached your limit of 3 ...",
                    "limit": 3,
                    "current": 3,
                }
            },
        )
        detail = _concurrency_limit_detail(resp)
        assert detail is not None
        assert detail["limit"] == 3

    def test_detects_flat_detail(self):
        """A non-nested {"error": ...} body is also tolerated."""
        resp = self._resp(429, json={"error": "CONCURRENCY_LIMIT_REACHED"})
        assert _concurrency_limit_detail(resp) is not None

    def test_ignores_transient_rate_limit_429(self):
        """A generic 429 with a string detail is a transient rate limit."""
        resp = self._resp(429, json={"detail": "Rate limited, slow down"})
        assert _concurrency_limit_detail(resp) is None

    def test_ignores_429_without_marker(self):
        """A 429 whose detail lacks the marker is not a concurrency limit."""
        resp = self._resp(429, json={"detail": {"error": "SOMETHING_ELSE"}})
        assert _concurrency_limit_detail(resp) is None

    def test_ignores_non_json_429(self):
        """A non-JSON 429 body never matches (and does not raise)."""
        resp = self._resp(429, raw=b"<html>too many requests</html>")
        assert _concurrency_limit_detail(resp) is None

    def test_ignores_non_429(self):
        """Only 429 responses can be a concurrency limit."""
        resp = self._resp(200, json={"id": "sandbox-abc"})
        assert _concurrency_limit_detail(resp) is None


class TestCloudSandboxConcurrencyLimit:
    """Tests that `_create_sandbox` surfaces the org concurrency limit as
    `ConcurrencyLimitReachedError` without retrying it."""

    @pytest.fixture
    def mock_run(self):
        run = MagicMock()
        run.sandbox_id = None
        run.bash_command_id = None
        return run

    @pytest.mark.asyncio
    async def test_create_sandbox_raises_and_does_not_retry(self, mock_run):
        """A concurrency-limit 429 raises ConcurrencyLimitReachedError on the
        first attempt — retrying cannot free a slot, so it must not be retried."""
        backend = CloudSandboxBackend(api_url="https://app.all-hands.dev", run=mock_run)

        req = httpx.Request("POST", "https://app.all-hands.dev/api/v1/sandboxes")
        resp = httpx.Response(
            429,
            request=req,
            json={
                "detail": {
                    "error": "CONCURRENCY_LIMIT_REACHED",
                    "message": "Reached limit of 3 concurrent conversations.",
                    "limit": 3,
                    "current": 3,
                }
            },
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)

        with pytest.raises(
            ConcurrencyLimitReachedError, match="concurrent conversations"
        ):
            await backend._create_sandbox(client, {"Authorization": "Bearer x"})

        # No retry: the sandbox API was hit exactly once.
        assert client.post.await_count == 1


class TestCloudSandboxProvisioningFailure:
    """A sandbox that never becomes ready raises SandboxProvisioningError with
    the runtime's reason, and is deleted before the error propagates."""

    API = "https://app.test"

    @staticmethod
    def _backend() -> CloudSandboxBackend:
        run = MagicMock()
        run.sandbox_id = None
        run.bash_command_id = None
        backend = CloudSandboxBackend(
            api_url=TestCloudSandboxProvisioningFailure.API, run=run
        )
        backend._poll_interval = 0
        return backend

    @staticmethod
    def _client(polled: list | Exception, calls: list[str], delete_status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "POST":
                return httpx.Response(200, json={"id": "sb1"})
            if request.method == "DELETE":
                return httpx.Response(delete_status, json=True)
            if isinstance(polled, Exception):
                raise polled
            return httpx.Response(200, json=polled)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    async def test_error_status_carries_runtime_detail_and_deletes(self):
        calls: list[str] = []
        polled = [
            {
                "id": "sb1",
                "status": "ERROR",
                "status_detail": "runtime_ingress:connection_error",
            }
        ]
        async with self._client(polled, calls) as client:
            with pytest.raises(SandboxProvisioningError) as exc_info:
                await self._backend()._create_and_wait(client, "key")

        exc = exc_info.value
        assert exc.sandbox_id == "sb1"
        assert exc.status == "ERROR"
        assert exc.reason == "runtime_ingress:connection_error"
        assert exc.code == "sandbox_error"
        assert not exc.timed_out
        assert "status=ERROR" in str(exc)
        assert "runtime_ingress:connection_error" in str(exc)
        assert calls[-1] == "DELETE"

    @pytest.mark.asyncio
    async def test_raw_pod_detail_is_summarized(self):
        calls: list[str] = []
        raw = "0/3 nodes are available: 3 Insufficient cpu. node pool-internal-7"
        polled = [{"id": "sb1", "status": "ERROR", "status_detail": raw}]
        async with self._client(polled, calls) as client:
            with pytest.raises(SandboxProvisioningError) as exc_info:
                await self._backend()._create_and_wait(client, "key")

        assert exc_info.value.reason == "insufficient_cpu"
        assert "pool-internal" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ready_timeout_deletes_sandbox(self):
        calls: list[str] = []
        polled = [{"id": "sb1", "status": "STARTING"}]
        backend = self._backend()
        setattr(backend, "_poll_interval", 0.001)
        async with self._client(polled, calls) as client:
            with pytest.raises(SandboxProvisioningError) as exc_info:
                await backend._create_and_wait(client, "key", ready_timeout=0.002)

        exc = exc_info.value
        assert exc.timed_out
        assert exc.status == "STARTING"
        assert exc.code == "sandbox_ready_timeout"
        assert calls[-1] == "DELETE"

    @pytest.mark.asyncio
    async def test_unknown_sandbox_is_missing(self):
        """The batch endpoint returns null for an id it doesn't know."""
        calls: list[str] = []
        async with self._client([None], calls) as client:
            with pytest.raises(SandboxProvisioningError) as exc_info:
                await self._backend()._create_and_wait(client, "key")

        assert exc_info.value.status == "MISSING"
        assert exc_info.value.code == "sandbox_missing"
        assert calls[-1] == "DELETE"

    @pytest.mark.asyncio
    async def test_failed_delete_does_not_mask_error(self):
        calls: list[str] = []
        polled = [{"id": "sb1", "status": "ERROR"}]
        async with self._client(polled, calls, delete_status=500) as client:
            with pytest.raises(SandboxProvisioningError) as exc_info:
                await self._backend()._create_and_wait(client, "key")

        assert exc_info.value.reason is None
        assert calls[-1] == "DELETE"

    @pytest.mark.asyncio
    async def test_other_failure_after_create_deletes_sandbox(self):
        calls: list[str] = []
        async with self._client(httpx.ConnectError("down"), calls) as client:
            with pytest.raises(httpx.ConnectError):
                await self._backend()._create_and_wait(client, "key")

        assert calls[-1] == "DELETE"


class TestSummarizeStatusDetail:
    @pytest.mark.parametrize(
        ("detail", "expected"),
        [
            (None, None),
            ("", None),
            ("runtime_ingress:timeout", "runtime_ingress:timeout"),
            ("runtime_ingress:http_status:503", "runtime_ingress:http_status:503"),
            ("runtime_ingress:bad value", "unclassified"),
            ("Back-off pulling image: ImagePullBackOff", "image_pull_error"),
            ("CreateContainerConfigError: secret missing", "container_config_error"),
            ("CreateContainerError", "container_create_error"),
            ("0/2 nodes: Insufficient memory", "insufficient_memory"),
            ("1 node(s) had untolerated taint {x: y}", "scheduling_constraint"),
            ("something else", "unclassified"),
        ],
    )
    def test_summary(self, detail, expected):
        assert summarize_status_detail(detail) == expected


class TestCloudSeedAgentProfile:
    """The run's agent profile is copied from the app server into the sandbox."""

    API = "https://app.example.com"
    AGENT = "https://sandbox.example.com"
    PROFILE_ID = "3d9a7654-c1e0-46a3-98ef-7795f2d466fc"

    def _profile(self, **overrides):
        return {
            "id": self.PROFILE_ID,
            "name": "subagents",
            "agent_kind": "openhands",
            "llm_profile_ref": "fast",
            "mcp_server_refs": ["github"],
            "tools": [{"name": "task_tool_set"}],
            **overrides,
        }

    async def _seed(self, profile, *, app_profiles=None, llm_profiles=None):
        run = MagicMock()
        run.automation.org_id = "org-1"
        backend = CloudSandboxBackend(api_url=self.API, run=run)
        ctx = ExecutionContext(
            agent_url=self.AGENT, session_key="session-1", sandbox_id="sbx-1"
        )
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            url = str(request.url)
            if url == f"{self.API}/api/agent-profiles":
                listed = app_profiles or [{"id": self.PROFILE_ID, "name": "subagents"}]
                return httpx.Response(200, json={"profiles": listed})
            if url == f"{self.API}/api/agent-profiles/subagents":
                return httpx.Response(
                    200, json={"name": "subagents", "profile": profile}
                )
            if url.startswith(f"{self.API}/api/v1/users/me"):
                return httpx.Response(
                    200,
                    json={
                        "llm_profiles": {
                            "profiles": llm_profiles
                            if llm_profiles is not None
                            else {"fast": {"model": "m", "api_key": "k"}}
                        },
                        "agent_settings": {
                            "mcp_config": {
                                "github": {"url": "https://gh.example/mcp"},
                                "slack": {"url": "https://slack.example/mcp"},
                            }
                        },
                    },
                )
            if url == f"{self.AGENT}/api/agent-profiles" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "profiles": [
                            {"id": "default-id", "name": "default"},
                            {"id": "sandbox-id", "name": "subagents"},
                        ]
                    },
                )
            if url.startswith(self.AGENT):
                return httpx.Response(200, json={})
            return httpx.Response(404)

        with patch(
            "openhands.automation.backends.cloud.get_api_key_for_automation_run",
            new_callable=AsyncMock,
            return_value="sk-user",
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                try:
                    result = await backend.seed_agent_profile(
                        client, ctx, self.PROFILE_ID
                    )
                except AgentProfileSeedError as exc:
                    result = exc
        return result, seen

    @staticmethod
    def _sandbox_writes(seen):
        return {
            (r.method, r.url.path): json.loads(r.content)
            for r in seen
            if r.url.host == "sandbox.example.com" and r.method != "GET"
        }

    async def test_copies_the_profile_and_what_it_references(self):
        profile = self._profile()
        result, seen = await self._seed(profile)

        assert result == "sandbox-id"
        writes = self._sandbox_writes(seen)
        assert writes == {
            ("POST", "/api/profiles/fast"): {
                "include_secrets": True,
                "llm": {"model": "m", "api_key": "k"},
            },
            ("PATCH", "/api/settings"): {
                "agent_settings_diff": {
                    "mcp_config": {"github": {"url": "https://gh.example/mcp"}}
                }
            },
            ("POST", "/api/agent-profiles/subagents"): profile,
        }
        for request in seen:
            if request.url.host == "sandbox.example.com":
                assert request.headers["X-Session-API-Key"] == "session-1"
            else:
                assert request.headers["Authorization"] == "Bearer sk-user"
                assert request.headers["X-Org-Id"] == "org-1"
        users_me = next(r for r in seen if r.url.path == "/api/v1/users/me")
        assert users_me.url.params["expose_secrets"] == "true"
        assert users_me.headers["X-Session-API-Key"] == "session-1"

    async def test_profile_without_mcp_refs_gets_every_server(self):
        _result, seen = await self._seed(self._profile(mcp_server_refs=None))

        mcp = self._sandbox_writes(seen)[("PATCH", "/api/settings")]
        assert set(mcp["agent_settings_diff"]["mcp_config"]) == {"github", "slack"}

    async def test_unknown_profile_writes_nothing_to_the_sandbox(self):
        result, seen = await self._seed(
            self._profile(), app_profiles=[{"id": "other", "name": "other"}]
        )

        assert isinstance(result, AgentProfileSeedError)
        assert str(result) == f"Agent profile `{self.PROFILE_ID}` not found"
        assert not [r for r in seen if r.url.host == "sandbox.example.com"]

    async def test_missing_llm_profile_writes_nothing_to_the_sandbox(self):
        result, seen = await self._seed(self._profile(), llm_profiles={})

        assert isinstance(result, AgentProfileSeedError)
        assert "LLM profile `fast`" in str(result)
        assert not [r for r in seen if r.url.host == "sandbox.example.com"]

    async def test_local_backend_launches_the_profile_it_already_has(self):
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:8000", api_key="k", run=MagicMock()
        )
        ctx = ExecutionContext(agent_url="http://localhost:8000", session_key="k")

        assert (
            await backend.seed_agent_profile(MagicMock(), ctx, self.PROFILE_ID)
            == self.PROFILE_ID
        )
