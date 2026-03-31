"""Tests for prompt-based automation creation endpoint."""

import io
import socket
import tarfile
import uuid
from unittest.mock import MagicMock

import pytest

from automation.models import Automation, TarballUpload, UploadStatus
from automation.prompt_router import _generate_tarball


# Test UUIDs matching mock_authenticated_user fixture
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


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
    """Tests for POST /v1/from-prompt endpoint."""

    @pytest.fixture
    def mock_file_store(self):
        """Create a mock file store."""
        store = MagicMock()
        store.write = MagicMock()
        return store

    async def test_create_from_prompt_success(
        self, async_client, async_session, mock_file_store
    ):
        """Valid request creates automation and upload, returns 201."""
        from automation.app import app
        from automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store

        payload = {
            "name": "My Prompt Automation",
            "prompt": "Create a file called hello.txt with 'Hello World' inside",
            "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

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

        # Verify file store was called
        mock_file_store.write.assert_called_once()

        # Clean up override
        app.dependency_overrides.pop(get_file_store, None)

    async def test_create_from_prompt_creates_upload_record(
        self, async_client, async_session, mock_file_store
    ):
        """Endpoint creates a TarballUpload record."""
        from automation.app import app
        from automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store

        payload = {
            "name": "Upload Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

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

        app.dependency_overrides.pop(get_file_store, None)

    async def test_create_from_prompt_creates_automation_record(
        self, async_client, async_session, mock_file_store
    ):
        """Endpoint creates an Automation record."""
        from automation.app import app
        from automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store

        payload = {
            "name": "Automation Record Test",
            "prompt": "Print hello",
            "trigger": {"type": "cron", "schedule": "30 10 * * 5"},
            "timeout": 300,
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

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

        app.dependency_overrides.pop(get_file_store, None)

    async def test_create_from_prompt_missing_name(self, async_client):
        """Missing name returns 422."""
        payload = {
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_missing_prompt(self, async_client):
        """Missing prompt returns 422."""
        payload = {
            "name": "Test",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_empty_prompt(self, async_client):
        """Empty prompt returns 422."""
        payload = {
            "name": "Test",
            "prompt": "",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_invalid_cron(self, async_client):
        """Invalid cron schedule returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "invalid-cron"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_missing_trigger(self, async_client):
        """Missing trigger returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_with_timeout(
        self, async_client, async_session, mock_file_store
    ):
        """Timeout value is properly set on automation."""
        from automation.app import app
        from automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store

        payload = {
            "name": "Timeout Test",
            "prompt": "Long running task",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            "timeout": 120,
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["timeout"] == 120

        app.dependency_overrides.pop(get_file_store, None)

    async def test_create_from_prompt_name_max_length(
        self, async_client, mock_file_store
    ):
        """Name exceeding max length returns 422."""
        payload = {
            "name": "x" * 501,  # Max is 500
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 422

    async def test_create_from_prompt_long_prompt(
        self, async_client, async_session, mock_file_store
    ):
        """Long prompt (within limits) is accepted."""
        from automation.app import app
        from automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store

        long_prompt = "x" * 10000  # Well within 50000 limit

        payload = {
            "name": "Long Prompt Test",
            "prompt": long_prompt,
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 201

        app.dependency_overrides.pop(get_file_store, None)

    async def test_create_from_prompt_storage_failure(
        self, async_client, async_session
    ):
        """Storage failure returns 500."""
        from automation.app import app
        from automation.storage import get_file_store

        failing_store = MagicMock()
        failing_store.write = MagicMock(side_effect=Exception("Storage unavailable"))

        app.dependency_overrides[get_file_store] = lambda: failing_store

        payload = {
            "name": "Storage Fail Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post("/v1/from-prompt", json=payload)

        assert response.status_code == 500

        app.dependency_overrides.pop(get_file_store, None)
