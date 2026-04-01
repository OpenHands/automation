"""Tests for preset-based automation creation endpoint."""

import io
import socket
import tarfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation.models import Automation, TarballUpload, UploadStatus
from automation.preset_router import _generate_tarball


# Test UUIDs matching mock_authenticated_user fixture
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")

# Path to preset files
PRESETS_DIR = Path(__file__).parent.parent / "automation" / "presets"


def _docker_available() -> bool:
    """Check if Docker is available for testcontainers."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect("/var/run/docker.sock")
        sock.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available for testcontainers",
)


class TestPresetFileSyntax:
    """Verify preset files have valid Python/shell syntax.

    These tests catch syntax errors before they break user automations.
    The preset files are excluded from linting, so this provides a safety net.
    """

    def test_prompt_preset_sdk_main_syntax(self):
        """Verify sdk_main.py has valid Python syntax."""
        sdk_main_path = PRESETS_DIR / "prompt" / "sdk_main.py"
        assert sdk_main_path.exists(), f"Preset file not found: {sdk_main_path}"

        source = sdk_main_path.read_text()
        # compile() raises SyntaxError if the code is invalid
        compile(source, str(sdk_main_path), "exec")

    def test_prompt_preset_setup_sh_exists(self):
        """Verify setup.sh exists and is not empty."""
        setup_sh_path = PRESETS_DIR / "prompt" / "setup.sh"
        assert setup_sh_path.exists(), f"Preset file not found: {setup_sh_path}"

        content = setup_sh_path.read_text()
        assert len(content) > 0, "setup.sh is empty"
        # Basic sanity check - should start with shebang or have pip install
        assert "pip install" in content or content.startswith("#"), (
            "setup.sh doesn't look like a valid shell script"
        )


class TestGenerateTarball:
    """Tests for the tarball generation function."""

    def test_generate_tarball_structure(self):
        """Generated tarball contains expected files."""
        prompt = "Write hello world to a file"
        tarball_bytes = _generate_tarball(prompt)

        # Verify it's a valid tarball
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "main.py" in names
            assert "prompt.txt" in names
            assert "setup.sh" in names

    def test_generate_tarball_prompt_content(self):
        """Generated tarball contains the user's prompt."""
        prompt = "Write a Python script that prints 'Hello, World!'"
        tarball_bytes = _generate_tarball(prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            prompt_file = tar.extractfile("prompt.txt")
            assert prompt_file is not None
            prompt_content = prompt_file.read().decode("utf-8")
            assert prompt_content == prompt

    def test_generate_tarball_main_py_content(self):
        """Generated tarball contains valid main.py with SDK code."""
        prompt = "Test prompt"
        tarball_bytes = _generate_tarball(prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            main_file = tar.extractfile("main.py")
            assert main_file is not None
            main_content = main_file.read().decode("utf-8")

            # Verify key SDK imports and patterns are present
            assert "from openhands.sdk import" in main_content
            assert "Agent" in main_content
            assert "Conversation" in main_content
            assert "OpenHandsCloudWorkspace" in main_content
            assert "get_mcp_config" in main_content
            assert "LLMSummarizingCondenser" in main_content
            assert "condenser=condenser" in main_content
            assert "prompt.txt" in main_content

    def test_generate_tarball_setup_sh_executable(self):
        """setup.sh in tarball has executable permissions."""
        prompt = "Test prompt"
        tarball_bytes = _generate_tarball(prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            setup_info = tar.getmember("setup.sh")
            # Check executable bit is set (0o755 includes 0o100 for owner execute)
            assert setup_info.mode & 0o100


@requires_docker
class TestCreateAutomationFromPrompt:
    """Tests for POST /v1/preset/prompt endpoint."""

    @pytest.fixture
    def mock_file_store(self):
        """Create a mock file store."""
        from collections.abc import AsyncIterator
        from unittest.mock import AsyncMock

        store = MagicMock()
        # Store captured content for test assertions
        store._captured_content = None

        # Mock write_stream to capture and return size
        async def mock_write_stream(
            path: str,
            stream: AsyncIterator[bytes],
            max_size: int | None = None,
            content_type: str = "application/octet-stream",
        ) -> int:
            content = b""
            async for chunk in stream:
                content += chunk
            store._captured_content = content
            return len(content)

        store.write_stream = AsyncMock(side_effect=mock_write_stream)
        store.delete = MagicMock()
        return store

    @pytest.fixture(autouse=True)
    def setup_file_store_override(self, mock_file_store):
        """Override file_store for all tests in this class."""
        from automation.app import app
        from automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store
        yield
        app.dependency_overrides.pop(get_file_store, None)

    async def test_create_from_prompt_success(
        self, async_client, async_session, mock_file_store
    ):
        """Valid request creates automation and upload, returns 201."""
        test_prompt = "Create a file called hello.txt with 'Hello World' inside"
        payload = {
            "name": "My Prompt Automation",
            "prompt": test_prompt,
            "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Prompt Automation"
        assert data["trigger"]["type"] == "cron"
        assert data["trigger"]["schedule"] == "0 9 * * 1"
        assert data["entrypoint"] == "python main.py"
        assert data["setup_script_path"] == "setup.sh"
        assert data["tarball_path"].startswith("oh-internal://uploads/")
        assert data["enabled"] is True
        assert "id" in data
        assert data["user_id"] == str(TEST_USER_ID)

        # Verify file store was called and tarball content is correct
        mock_file_store.write_stream.assert_called_once()
        call_args = mock_file_store.write_stream.call_args
        assert call_args.kwargs["path"].startswith("uploads/")

        # Verify tarball content from captured bytes
        tarball_bytes = mock_file_store._captured_content
        assert tarball_bytes is not None
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            assert "main.py" in tar.getnames()
            assert "prompt.txt" in tar.getnames()
            assert "setup.sh" in tar.getnames()

            # Verify prompt content matches what was sent
            prompt_file = tar.extractfile("prompt.txt")
            assert prompt_file is not None
            assert prompt_file.read().decode() == test_prompt

    async def test_create_from_prompt_creates_upload_record(
        self, async_client, async_session, mock_file_store
    ):
        """Endpoint creates a TarballUpload record."""
        payload = {
            "name": "Upload Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 201
        data = response.json()

        # Extract upload ID from tarball_path
        tarball_path = data["tarball_path"]
        upload_id_str = tarball_path.replace("oh-internal://uploads/", "")
        upload_id = uuid.UUID(upload_id_str)

        # Verify upload record exists
        from sqlalchemy import select

        result = await async_session.execute(
            select(TarballUpload).where(TarballUpload.id == upload_id)
        )
        upload = result.scalars().first()
        assert upload is not None
        assert upload.status == UploadStatus.COMPLETED
        assert upload.user_id == TEST_USER_ID
        assert upload.org_id == TEST_ORG_ID

    async def test_create_from_prompt_creates_automation_record(
        self, async_client, async_session, mock_file_store
    ):
        """Endpoint creates an Automation record."""
        payload = {
            "name": "Automation Record Test",
            "prompt": "Print hello",
            "trigger": {"type": "cron", "schedule": "30 10 * * 5"},
            "timeout": 300,
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 201
        data = response.json()
        automation_id = uuid.UUID(data["id"])

        # Verify automation record exists
        from sqlalchemy import select

        result = await async_session.execute(
            select(Automation).where(Automation.id == automation_id)
        )
        automation = result.scalars().first()
        assert automation is not None
        assert automation.name == "Automation Record Test"
        assert automation.entrypoint == "python main.py"
        assert automation.setup_script_path == "setup.sh"
        assert automation.timeout == 300
        assert automation.user_id == TEST_USER_ID
        assert automation.org_id == TEST_ORG_ID

    async def test_create_from_prompt_missing_name(self, async_client):
        """Missing name returns 422."""
        payload = {
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_missing_prompt(self, async_client):
        """Missing prompt returns 422."""
        payload = {
            "name": "Test",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_empty_prompt(self, async_client):
        """Empty prompt returns 422."""
        payload = {
            "name": "Test",
            "prompt": "",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_invalid_cron(self, async_client):
        """Invalid cron schedule returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "invalid-cron"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_missing_trigger(self, async_client):
        """Missing trigger returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_with_timeout(
        self, async_client, async_session, mock_file_store
    ):
        """Timeout value is properly set on automation."""
        payload = {
            "name": "Timeout Test",
            "prompt": "Long running task",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            "timeout": 120,
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["timeout"] == 120

    async def test_create_from_prompt_name_max_length(self, async_client):
        """Name exceeding max length returns 422."""
        payload = {
            "name": "x" * 501,  # Max is 500
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_long_prompt(
        self, async_client, async_session, mock_file_store
    ):
        """Long prompt (within limits) is accepted."""
        long_prompt = "x" * 10000  # Well within 50000 limit

        payload = {
            "name": "Long Prompt Test",
            "prompt": long_prompt,
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 201

    async def test_create_from_prompt_storage_failure(
        self, async_client, async_session, mock_file_store
    ):
        """Storage failure returns 500."""
        from unittest.mock import AsyncMock

        # Configure the mock to fail on write_stream
        mock_file_store.write_stream = AsyncMock(
            side_effect=Exception("Storage unavailable")
        )

        payload = {
            "name": "Storage Fail Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/preset/prompt", json=payload)

        assert response.status_code == 500
