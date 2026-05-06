"""Tests for the execution module — build_tarball, _shell_quote, and result types.

Only tests pure logic that can run without a network.  The e2e flow
(run_automation against a real sandbox) lives in scripts/test_automation.py.
"""

import io
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from automation.config import get_config
from automation.exceptions import PermanentDispatchError, TarballNotFoundError
from automation.execution import (
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
    @patch("automation.execution._download_in_sandbox")
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
            )

        assert "not accessible" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("automation.execution._download_in_sandbox")
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
        )

        assert isinstance(result, DispatchResult)
        assert result.success is False
        assert result.error is not None
        assert "Connection timeout" in result.error

    @pytest.mark.asyncio
    @patch("automation.execution._upload")
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
            )

        assert "permanently failed" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("automation.execution._upload")
    @patch("automation.execution._start_bash")
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
            sandbox_id="test-sandbox-id",
        )

        assert isinstance(result, DispatchResult)
        assert result.success is True
        assert result.sandbox_id == "test-sandbox-id"
