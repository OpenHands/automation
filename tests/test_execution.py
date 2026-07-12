"""Tests for the execution module — build_tarball, _shell_quote, and result types.

Only tests pure logic that can run without a network.  The e2e flow
(run_automation against a real sandbox) lives in scripts/test_automation.py.
"""

import io
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.automation.config import get_config
from openhands.automation.constants import TARBALL_PATH
from openhands.automation.exceptions import PermanentDispatchError, TarballNotFoundError
from openhands.automation.execution import (
    DEFAULT_WORK_DIR,
    AutomationResult,
    DispatchResult,
    _shell_quote,
    _upload,
    build_tarball,
    execute_in_context,
)


class TestBuildTarball:
    def test_produces_valid_tarball(self):
        tb = build_tarball({"hello.txt": "world", "bin.dat": b"\x00\x01"})
        with tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz") as tar:
            names = sorted(tar.getnames())
            assert names == ["bin.dat", "hello.txt"]
            hello = tar.extractfile("hello.txt")
            assert hello is not None
            assert hello.read() == b"world"
            bindat = tar.extractfile("bin.dat")
            assert bindat is not None
            assert bindat.read() == b"\x00\x01"

    def test_empty_files(self):
        tb = build_tarball({})
        with tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz") as tar:
            assert tar.getnames() == []

    def test_setup_and_entrypoint(self):
        tb = build_tarball(
            {
                "setup.sh": "#!/bin/bash\npip install requests\n",
                "run.py": 'print("ok")\n',
            }
        )
        with tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz") as tar:
            assert "setup.sh" in tar.getnames()
            assert "run.py" in tar.getnames()
            setup_file = tar.extractfile("setup.sh")
            assert setup_file is not None
            setup = setup_file.read().decode()
            assert "pip install" in setup


class TestShellQuote:
    def test_simple_string(self):
        assert _shell_quote("hello") == "'hello'"

    def test_string_with_spaces(self):
        assert _shell_quote("hello world") == "'hello world'"

    def test_string_with_single_quotes(self):
        assert _shell_quote("it's") == "'it'\\''s'"

    def test_empty_string(self):
        assert _shell_quote("") == "''"

    def test_special_characters(self):
        assert _shell_quote("$HOME") == "'$HOME'"


class TestAutomationResult:
    """Tests for AutomationResult (blocking execution result)."""

    def test_frozen_dataclass(self):
        r = AutomationResult(success=True, sandbox_id="sb-1", exit_code=0, stdout="ok")
        assert r.success is True
        assert r.sandbox_id == "sb-1"
        assert r.exit_code == 0
        assert r.stdout == "ok"
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]

    def test_with_error(self):
        r = AutomationResult(
            success=False,
            sandbox_id="sb-1",
            exit_code=1,
            stderr="error",
            error="Failed",
        )
        assert r.success is False
        assert r.exit_code == 1
        assert r.stderr == "error"
        assert r.error == "Failed"


class TestDispatchResult:
    """Tests for DispatchResult (fire-and-forget execution result)."""

    def test_frozen_dataclass(self):
        r = DispatchResult(success=True, sandbox_id="sb-1")
        assert r.success is True
        assert r.sandbox_id == "sb-1"
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]

    def test_with_error(self):
        r = DispatchResult(success=False, sandbox_id="sb-1", error="Failed to start")
        assert r.success is False
        assert r.error == "Failed to start"


class TestAutomationTarballSource:
    """Tests for tarball_source parameter."""

    def test_tarball_source_accepts_bytes(self):
        """tarball_source accepts bytes (will be uploaded)."""
        # This just validates the type - actual execution would need mocking
        source: bytes | str = b"test tarball content"
        assert isinstance(source, bytes)

    def test_tarball_source_accepts_str(self):
        """tarball_source accepts str URL (will be downloaded in sandbox)."""
        source: bytes | str = "https://example.com/file.tar.gz"
        assert isinstance(source, str)


class TestExternalDownloadConstants:
    """Tests for external download configuration constants."""

    def test_timeout_is_reasonable(self):
        """External download timeout should be reasonable (60-300s)."""
        timeout = get_config().sandbox.external_download_timeout
        assert 60 <= timeout <= 300

    def test_max_filesize_is_reasonable(self):
        """Max filesize should be reasonable (10MB - 500MB)."""
        max_filesize = get_config().sandbox.external_max_filesize
        assert 10 * 1024 * 1024 <= max_filesize <= 500 * 1024 * 1024


class TestUploadUsesQueryParams:
    """Tests for _upload using query parameters instead of path parameters.

    This prevents URL normalization issues with proxies (e.g., Traefik) that
    collapse double-slashes in paths. See:
    - https://github.com/All-Hands-AI/OpenHands/commit/a14158e
    - https://github.com/OpenHands/software-agent-sdk/pull/2404
    """

    @pytest.mark.asyncio
    async def test_upload_uses_query_param_for_path(self):
        """_upload should use ?path= query param, not path in URL."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        await _upload(
            client=mock_client,
            agent_url="https://agent.example.com",
            session_key="test-session-key",
            data=b"test data",
            dest="/tmp/automation.tar.gz",
        )

        # Verify post was called with query param, not path param
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args

        url = call_args[0][0]
        # URL should use query param format
        assert "?path=" in url, f"Expected query param in URL, got: {url}"
        assert "/tmp/automation.tar.gz" not in url.split("?")[0], (
            f"Path should not be in URL path segment: {url}"
        )
        # Verify the path is properly encoded in query string
        assert (
            "path=%2Ftmp%2Fautomation.tar.gz" in url
            or "path=/tmp/automation.tar.gz" in url
        )

    @pytest.mark.asyncio
    async def test_upload_preserves_absolute_path(self):
        """_upload should preserve leading slash in path via query param."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        await _upload(
            client=mock_client,
            agent_url="https://agent.example.com",
            session_key="test-session-key",
            data=b"test data",
            dest="/workspace/file.txt",
        )

        url = mock_client.post.call_args[0][0]
        # The path in query param should preserve the leading slash
        # (either URL-encoded as %2F or literal /)
        assert "%2Fworkspace" in url or "/workspace" in url.split("?")[1]


class TestExecuteInContextErrors:
    """Tests for execute_in_context error handling."""

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._download_in_sandbox")
    async def test_reraises_permanent_error(self, mock_download_in_sandbox):
        """PermanentDispatchError is re-raised for caller to handle."""
        mock_download_in_sandbox.side_effect = TarballNotFoundError(
            "External tarball URL is not accessible"
        )

        mock_client = AsyncMock()
        with pytest.raises(TarballNotFoundError) as exc_info:
            await execute_in_context(
                client=mock_client,
                agent_url="https://agent.example.com",
                session_key="test-session-key",
                entrypoint="python main.py",
                tarball_source="https://example.com/missing.tar.gz",
                work_dir=DEFAULT_WORK_DIR,
            )

        assert "not accessible" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._download_in_sandbox")
    async def test_transient_error_returns_dispatch_result(
        self, mock_download_in_sandbox
    ):
        """Non-permanent errors return DispatchResult with success=False."""
        mock_download_in_sandbox.side_effect = RuntimeError("Connection timeout")

        mock_client = AsyncMock()
        result = await execute_in_context(
            client=mock_client,
            agent_url="https://agent.example.com",
            session_key="test-session-key",
            entrypoint="python main.py",
            tarball_source="https://example.com/file.tar.gz",
            work_dir=DEFAULT_WORK_DIR,
        )

        assert isinstance(result, DispatchResult)
        assert result.success is False
        assert result.error is not None
        assert "Connection timeout" in result.error

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._start_bash", new_callable=AsyncMock)
    @patch("openhands.automation.execution._upload", new_callable=AsyncMock)
    async def test_custom_timeout_is_passed_to_bash(self, mock_upload, mock_start_bash):
        """execute_in_context passes above-default custom timeouts to bash."""
        mock_start_bash.return_value = "cmd-1"

        result = await execute_in_context(
            client=AsyncMock(),
            agent_url="https://agent.example.com",
            session_key="test-session-key",
            entrypoint="python main.py",
            tarball_source=b"test tarball",
            work_dir=DEFAULT_WORK_DIR,
            timeout=1200,
        )

        assert result.success is True
        mock_upload.assert_awaited_once()
        assert mock_start_bash.await_args.kwargs["timeout"] == 1200

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    async def test_permanent_error_with_bytes_tarball_reraises(self, mock_upload):
        """PermanentDispatchError during upload is also re-raised."""
        mock_upload.side_effect = PermanentDispatchError("Upload permanently failed")

        mock_client = AsyncMock()
        with pytest.raises(PermanentDispatchError) as exc_info:
            await execute_in_context(
                client=mock_client,
                agent_url="https://agent.example.com",
                session_key="test-session-key",
                entrypoint="python main.py",
                tarball_source=b"fake tarball bytes",
                work_dir=DEFAULT_WORK_DIR,
            )

        assert "permanently failed" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    @patch("openhands.automation.execution._start_bash")
    async def test_success_returns_dispatch_result(self, mock_start_bash, mock_upload):
        """Successful execution returns DispatchResult with success=True."""
        mock_upload.return_value = None
        mock_start_bash.return_value = "cmd-123"

        mock_client = AsyncMock()
        result = await execute_in_context(
            client=mock_client,
            agent_url="https://agent.example.com",
            session_key="test-session-key",
            entrypoint="python main.py",
            tarball_source=b"fake tarball bytes",
            work_dir=DEFAULT_WORK_DIR,
            sandbox_id="test-sandbox-id",
        )

        assert isinstance(result, DispatchResult)
        assert result.success is True
        assert result.sandbox_id == "test-sandbox-id"


class TestPerRunTarballPath:
    """Tests that execute_in_context uses an isolated per-run tarball path.

    In sandboxless/local mode all automation runs share the same host
    filesystem.  Using the shared TARBALL_PATH constant causes a race
    condition when two automations fire at the same cron tick: the second
    upload overwrites the first before extraction, so both runs execute the
    wrong script.

    The fix derives a unique path from the run_id so concurrent uploads
    cannot collide.
    """

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    @patch("openhands.automation.execution._start_bash")
    async def test_bytes_upload_uses_per_run_path(self, mock_start_bash, mock_upload):
        """When run_id is provided, tarball is uploaded to a run-scoped path."""
        mock_upload.return_value = None
        mock_start_bash.return_value = "cmd-abc"
        run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        await execute_in_context(
            client=AsyncMock(),
            agent_url="https://agent.example.com",
            session_key="key",
            entrypoint="python main.py",
            tarball_source=b"fake bytes",
            work_dir=DEFAULT_WORK_DIR,
            run_id=run_id,
        )

        uploaded_dest = mock_upload.call_args.args[4]  # (client, url, key, data, dest)
        assert uploaded_dest == f"/tmp/automation-{run_id}.tar.gz"
        assert uploaded_dest != TARBALL_PATH

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._download_in_sandbox")
    @patch("openhands.automation.execution._start_bash")
    async def test_url_download_uses_per_run_path(
        self, mock_start_bash, mock_download_in_sandbox
    ):
        """When run_id is provided, URL tarball is downloaded to a run-scoped path."""
        mock_download_in_sandbox.return_value = None
        mock_start_bash.return_value = "cmd-abc"
        run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        await execute_in_context(
            client=AsyncMock(),
            agent_url="https://agent.example.com",
            session_key="key",
            entrypoint="python main.py",
            tarball_source="https://example.com/script.tar.gz",
            work_dir=DEFAULT_WORK_DIR,
            run_id=run_id,
        )

        download_dest = mock_download_in_sandbox.call_args.args[4]
        assert download_dest == f"/tmp/automation-{run_id}.tar.gz"
        assert download_dest != TARBALL_PATH

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    @patch("openhands.automation.execution._start_bash")
    async def test_bash_cmd_uses_per_run_path_and_cleans_up(
        self, mock_start_bash, mock_upload
    ):
        """The bash command extracts from the per-run path and removes it afterwards."""
        mock_upload.return_value = None
        mock_start_bash.return_value = "cmd-abc"
        run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        expected_path = f"/tmp/automation-{run_id}.tar.gz"

        await execute_in_context(
            client=AsyncMock(),
            agent_url="https://agent.example.com",
            session_key="key",
            entrypoint="python main.py",
            tarball_source=b"fake bytes",
            work_dir=DEFAULT_WORK_DIR,
            run_id=run_id,
        )

        bash_cmd = mock_start_bash.call_args.args[3]  # (client, url, key, command)
        assert f"tar xzf {expected_path}" in bash_cmd
        assert f"rm -f {expected_path}" in bash_cmd
        assert TARBALL_PATH not in bash_cmd

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    @patch("openhands.automation.execution._start_bash")
    async def test_no_run_id_falls_back_to_shared_constant(
        self, mock_start_bash, mock_upload
    ):
        """Without a run_id the shared TARBALL_PATH constant is used as a fallback."""
        mock_upload.return_value = None
        mock_start_bash.return_value = "cmd-abc"

        await execute_in_context(
            client=AsyncMock(),
            agent_url="https://agent.example.com",
            session_key="key",
            entrypoint="python main.py",
            tarball_source=b"fake bytes",
            work_dir=DEFAULT_WORK_DIR,
            run_id=None,
        )

        uploaded_dest = mock_upload.call_args.args[4]
        assert uploaded_dest == TARBALL_PATH
        bash_cmd = mock_start_bash.call_args.args[3]
        assert f"tar xzf {TARBALL_PATH}" in bash_cmd

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    @patch("openhands.automation.execution._start_bash")
    async def test_concurrent_runs_use_distinct_paths(
        self, mock_start_bash, mock_upload
    ):
        """Two concurrent calls with different run_ids upload to different paths.

        This is the core race condition that the fix prevents: if both runs
        wrote to the same path the second upload would silently overwrite the
        first, causing both runs to execute the wrong script.
        """
        import asyncio

        mock_upload.return_value = None
        mock_start_bash.return_value = "cmd-abc"

        run_id_a = "11111111-0000-0000-0000-000000000000"
        run_id_b = "22222222-0000-0000-0000-000000000000"

        await asyncio.gather(
            execute_in_context(
                client=AsyncMock(),
                agent_url="https://agent.example.com",
                session_key="key",
                entrypoint="python main.py",
                tarball_source=b"script A",
                work_dir=DEFAULT_WORK_DIR,
                run_id=run_id_a,
            ),
            execute_in_context(
                client=AsyncMock(),
                agent_url="https://agent.example.com",
                session_key="key",
                entrypoint="python main.py",
                tarball_source=b"script B",
                work_dir=DEFAULT_WORK_DIR,
                run_id=run_id_b,
            ),
        )

        upload_dests = {c.args[4] for c in mock_upload.call_args_list}
        assert f"/tmp/automation-{run_id_a}.tar.gz" in upload_dests
        assert f"/tmp/automation-{run_id_b}.tar.gz" in upload_dests
        assert len(upload_dests) == 2, "Each run must upload to its own unique path"

    @pytest.mark.asyncio
    @patch("openhands.automation.execution._upload")
    @patch("openhands.automation.execution._start_bash")
    async def test_run_id_with_slash_falls_back_to_shared_constant(
        self, mock_start_bash, mock_upload
    ):
        """A run_id containing '/' falls back to TARBALL_PATH (path traversal guard)."""
        mock_upload.return_value = None
        mock_start_bash.return_value = "cmd-abc"

        await execute_in_context(
            client=AsyncMock(),
            agent_url="https://agent.example.com",
            session_key="key",
            entrypoint="python main.py",
            tarball_source=b"fake bytes",
            work_dir=DEFAULT_WORK_DIR,
            run_id="../../etc/passwd",
        )

        uploaded_dest = mock_upload.call_args.args[4]
        assert uploaded_dest == TARBALL_PATH
        assert "etc/passwd" not in uploaded_dest
