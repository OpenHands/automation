"""Tests for Temporal activities.

Uses ActivityEnvironment to test activities in isolation without a Worker.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from automation.temporal.activities import (
    cleanup_sandbox,
    create_sandbox,
    download_tarball,
    execute_entrypoint,
    get_api_key,
    upload_tarball,
)
from automation.temporal.types import (
    CleanupSandboxInput,
    CreateSandboxInput,
    DownloadTarballInput,
    ExecuteEntrypointInput,
    ExecutionResult,
    GetApiKeyInput,
    SandboxInfo,
    UploadTarballInput,
)


class TestGetApiKeyActivity:
    """Tests for get_api_key activity."""

    @pytest.fixture
    def activity_env(self) -> ActivityEnvironment:
        return ActivityEnvironment()

    @pytest.fixture
    def input(self) -> GetApiKeyInput:
        return GetApiKeyInput(
            user_id=str(uuid.uuid4()),
            org_id=str(uuid.uuid4()),
            run_id="run-123",
        )

    @pytest.mark.asyncio
    async def test_get_api_key_success(
        self, activity_env: ActivityEnvironment, input: GetApiKeyInput
    ):
        """Test successful API key retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "sk-test-key-12345"}
        mock_response.raise_for_status = MagicMock()

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await activity_env.run(get_api_key, input)

            assert result == "sk-test-key-12345"

    @pytest.mark.asyncio
    async def test_get_api_key_failure(
        self, activity_env: ActivityEnvironment, input: GetApiKeyInput
    ):
        """Test API key retrieval failure."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=mock_response
        )

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            with pytest.raises(httpx.HTTPStatusError):
                await activity_env.run(get_api_key, input)


class TestCreateSandboxActivity:
    """Tests for create_sandbox activity."""

    @pytest.fixture
    def activity_env(self) -> ActivityEnvironment:
        return ActivityEnvironment()

    @pytest.fixture
    def input(self) -> CreateSandboxInput:
        return CreateSandboxInput(
            api_url="https://api.example.com",
            api_key="sk-test-key",
            run_id="run-123",
        )

    @pytest.mark.asyncio
    async def test_create_sandbox_success(
        self, activity_env: ActivityEnvironment, input: CreateSandboxInput
    ):
        """Test successful sandbox creation."""
        sandbox_id = "sandbox-abc123"

        # Mock sandbox creation response
        create_response = MagicMock()
        create_response.status_code = 200
        create_response.json.return_value = {"sandbox_id": sandbox_id}

        # Mock sandbox status poll response (immediately running)
        poll_response = MagicMock()
        poll_response.status_code = 200
        poll_response.json.return_value = [
            {
                "sandbox_id": sandbox_id,
                "status": "RUNNING",
                "session_api_key": "session-key-xyz",
                "exposed_urls": [
                    {"name": "AGENT_SERVER", "url": "https://agent.example.com"}
                ],
            }
        ]

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_instance.get = AsyncMock(return_value=poll_response)

            result = await activity_env.run(create_sandbox, input)

            assert isinstance(result, SandboxInfo)
            assert result.sandbox_id == sandbox_id
            assert result.agent_url == "https://agent.example.com"
            assert result.session_key == "session-key-xyz"

    @pytest.mark.asyncio
    async def test_create_sandbox_creation_fails(
        self, activity_env: ActivityEnvironment, input: CreateSandboxInput
    ):
        """Test sandbox creation failure."""
        create_response = MagicMock()
        create_response.status_code = 500
        create_response.text = "Internal server error"
        create_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=create_response
        )

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = AsyncMock(return_value=create_response)

            with pytest.raises(httpx.HTTPStatusError):
                await activity_env.run(create_sandbox, input)


class TestDownloadTarballActivity:
    """Tests for download_tarball activity."""

    @pytest.fixture
    def activity_env(self) -> ActivityEnvironment:
        return ActivityEnvironment()

    @pytest.fixture
    def input(self) -> DownloadTarballInput:
        return DownloadTarballInput(
            upload_id="upload-123",
            run_id="run-123",
        )

    @pytest.mark.asyncio
    async def test_download_tarball_success(
        self, activity_env: ActivityEnvironment, input: DownloadTarballInput
    ):
        """Test successful internal tarball download."""
        tarball_content = b"mock tarball content"

        with patch(
            "automation.temporal.activities.get_file_store"
        ) as mock_get_store:
            mock_store = AsyncMock()
            mock_store.read.return_value = tarball_content
            mock_get_store.return_value = mock_store

            result = await activity_env.run(download_tarball, input)

            assert result == tarball_content
            mock_store.read.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_tarball_not_found(
        self, activity_env: ActivityEnvironment, input: DownloadTarballInput
    ):
        """Test tarball download when file not found."""
        with patch(
            "automation.temporal.activities.get_file_store"
        ) as mock_get_store:
            mock_store = AsyncMock()
            mock_store.read.side_effect = FileNotFoundError("Tarball not found")
            mock_get_store.return_value = mock_store

            with pytest.raises(FileNotFoundError):
                await activity_env.run(download_tarball, input)


class TestUploadTarballActivity:
    """Tests for upload_tarball activity."""

    @pytest.fixture
    def activity_env(self) -> ActivityEnvironment:
        return ActivityEnvironment()

    @pytest.fixture
    def sandbox_info(self) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id="sandbox-123",
            agent_url="https://agent.example.com",
            session_key="session-key",
            api_key="sk-test-key",
        )

    @pytest.fixture
    def input(self, sandbox_info: SandboxInfo) -> UploadTarballInput:
        return UploadTarballInput(
            sandbox_info=sandbox_info,
            tarball_data=b"mock tarball content",
            run_id="run-123",
        )

    @pytest.mark.asyncio
    async def test_upload_tarball_with_content(
        self, activity_env: ActivityEnvironment, input: UploadTarballInput
    ):
        """Test uploading tarball content to sandbox."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = AsyncMock(return_value=mock_response)

            await activity_env.run(upload_tarball, input)

            # Verify upload was called
            mock_instance.post.assert_called()

    @pytest.mark.asyncio
    async def test_upload_tarball_external_url(
        self, activity_env: ActivityEnvironment, sandbox_info: SandboxInfo
    ):
        """Test triggering external tarball download in sandbox."""
        input = UploadTarballInput(
            sandbox_info=sandbox_info,
            tarball_data=None,  # External URL - no content
            tarball_url="https://example.com/tarball.tar.gz",
            run_id="run-123",
        )

        # Mock bash command execution for curl
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "cmd-123"}
        mock_response.raise_for_status = MagicMock()

        # Mock bash result polling
        bash_result_response = MagicMock()
        bash_result_response.status_code = 200
        bash_result_response.json.return_value = {"exit_code": 0}

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.get = AsyncMock(return_value=bash_result_response)

            await activity_env.run(upload_tarball, input)


class TestExecuteEntrypointActivity:
    """Tests for execute_entrypoint activity."""

    @pytest.fixture
    def activity_env(self) -> ActivityEnvironment:
        return ActivityEnvironment()

    @pytest.fixture
    def sandbox_info(self) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id="sandbox-123",
            agent_url="https://agent.example.com",
            session_key="session-key",
            api_key="sk-test-key",
        )

    @pytest.fixture
    def input(self, sandbox_info: SandboxInfo) -> ExecuteEntrypointInput:
        return ExecuteEntrypointInput(
            sandbox_info=sandbox_info,
            entrypoint="python main.py",
            env_vars={"API_KEY": "test-key"},
            timeout_seconds=300,
            run_id="run-123",
        )

    @pytest.mark.asyncio
    async def test_execute_entrypoint_success(
        self, activity_env: ActivityEnvironment, input: ExecuteEntrypointInput
    ):
        """Test successful entrypoint execution."""
        # Mock bash start response
        start_response = MagicMock()
        start_response.status_code = 200
        start_response.json.return_value = {"id": "cmd-123"}
        start_response.raise_for_status = MagicMock()

        # Mock bash result - command completed successfully
        result_response = MagicMock()
        result_response.status_code = 200
        result_response.json.return_value = {
            "exit_code": 0,
            "stdout": "Success output",
            "stderr": "",
        }

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = AsyncMock(return_value=start_response)
            mock_instance.get = AsyncMock(return_value=result_response)

            result = await activity_env.run(execute_entrypoint, input)

            assert isinstance(result, ExecutionResult)
            assert result.success is True
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_execute_entrypoint_failure(
        self, activity_env: ActivityEnvironment, input: ExecuteEntrypointInput
    ):
        """Test failed entrypoint execution."""
        # Mock bash start response
        start_response = MagicMock()
        start_response.status_code = 200
        start_response.json.return_value = {"id": "cmd-123"}
        start_response.raise_for_status = MagicMock()

        # Mock bash result - command failed
        result_response = MagicMock()
        result_response.status_code = 200
        result_response.json.return_value = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "Error: something went wrong",
        }

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = AsyncMock(return_value=start_response)
            mock_instance.get = AsyncMock(return_value=result_response)

            result = await activity_env.run(execute_entrypoint, input)

            assert isinstance(result, ExecutionResult)
            assert result.success is False
            assert result.exit_code == 1


class TestCleanupSandboxActivity:
    """Tests for cleanup_sandbox activity."""

    @pytest.fixture
    def activity_env(self) -> ActivityEnvironment:
        return ActivityEnvironment()

    @pytest.fixture
    def input(self) -> CleanupSandboxInput:
        return CleanupSandboxInput(
            api_url="https://api.example.com",
            api_key="sk-test-key",
            sandbox_id="sandbox-123",
            run_id="run-123",
        )

    @pytest.mark.asyncio
    async def test_cleanup_sandbox_success(
        self, activity_env: ActivityEnvironment, input: CleanupSandboxInput
    ):
        """Test successful sandbox cleanup."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.delete = AsyncMock(return_value=mock_response)

            await activity_env.run(cleanup_sandbox, input)

            mock_instance.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_sandbox_failure_ignored(
        self, activity_env: ActivityEnvironment, input: CleanupSandboxInput
    ):
        """Test cleanup failure is handled gracefully."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not found"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not found", request=MagicMock(), response=mock_response
        )

        with patch("automation.temporal.activities.httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.delete = AsyncMock(return_value=mock_response)

            # Should not raise - cleanup failures are logged but not propagated
            await activity_env.run(cleanup_sandbox, input)
