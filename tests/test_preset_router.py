"""Tests for preset-based automation creation endpoint."""

import io
import json
import socket
import tarfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openhands.sdk.plugin import PluginSource
from openhands.workspace import RepoSource

from automation.models import Automation, TarballUpload, UploadStatus
from automation.preset_router import (
    _generate_plugin_tarball,
    _generate_tarball,
)


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

    def test_plugin_preset_sdk_main_syntax(self):
        """Verify plugin sdk_main.py has valid Python syntax."""
        sdk_main_path = PRESETS_DIR / "plugin" / "sdk_main.py"
        assert sdk_main_path.exists(), f"Preset file not found: {sdk_main_path}"

        source = sdk_main_path.read_text()
        # compile() raises SyntaxError if the code is invalid
        compile(source, str(sdk_main_path), "exec")

    def test_plugin_preset_setup_sh_exists(self):
        """Verify plugin setup.sh exists and is not empty."""
        setup_sh_path = PRESETS_DIR / "plugin" / "setup.sh"
        assert setup_sh_path.exists(), f"Preset file not found: {setup_sh_path}"

        content = setup_sh_path.read_text()
        assert len(content) > 0, "setup.sh is empty"
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
            # Note: load_skills.py and clone_repos.py are no longer needed
            # as the SDK workspace now provides these methods directly

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
            assert "Conversation" in main_content
            assert "OpenHandsCloudWorkspace" in main_content
            assert "get_mcp_config" in main_content
            assert "get_default_agent" in main_content
            assert "model_copy" in main_content
            assert "prompt.txt" in main_content

    def test_generate_tarball_setup_sh_executable(self):
        """setup.sh in tarball has executable permissions."""
        prompt = "Test prompt"
        tarball_bytes = _generate_tarball(prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            setup_info = tar.getmember("setup.sh")
            # Check executable bit is set (0o755 includes 0o100 for owner execute)
            assert setup_info.mode & 0o100

    def test_generate_tarball_without_repos(self):
        """Generated tarball without repos does not include repos_config.json."""
        prompt = "Test prompt"
        tarball_bytes = _generate_tarball(prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "repos_config.json" not in names

    def test_generate_tarball_with_repos(self):
        """Generated tarball with repos includes repos config."""
        prompt = "Test prompt"
        repos = [
            RepoSource(url="owner/repo1"),
            RepoSource(url="owner/repo2", ref="main"),
        ]
        tarball_bytes = _generate_tarball(prompt, repos=repos)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "repos_config.json" in names
            # Note: clone_repos.py is no longer included - SDK handles cloning
            assert "clone_repos.py" not in names

            # Verify repos config content
            repos_file = tar.extractfile("repos_config.json")
            assert repos_file is not None
            repos_config = json.load(repos_file)
            assert len(repos_config) == 2
            assert repos_config[0]["url"] == "owner/repo1"
            assert "ref" not in repos_config[0]  # None excluded
            assert repos_config[1]["url"] == "owner/repo2"
            assert repos_config[1]["ref"] == "main"


class TestRepoSource:
    """Tests for RepoSource model."""

    def test_repo_source_string_normalization(self):
        """RepoSource accepts a string and normalizes it to object."""
        repo = RepoSource.model_validate("owner/repo")
        assert repo.url == "owner/repo"
        assert repo.ref is None

    def test_repo_source_with_ref(self):
        """RepoSource accepts ref parameter."""
        repo = RepoSource(url="owner/repo", ref="v1.0.0")
        assert repo.url == "owner/repo"
        assert repo.ref == "v1.0.0"

    def test_repo_source_full_url(self):
        """RepoSource accepts full URLs."""
        repo = RepoSource(url="https://github.com/owner/repo")
        assert repo.url == "https://github.com/owner/repo"

    def test_repo_source_gitlab_url(self):
        """RepoSource accepts GitLab URLs."""
        repo = RepoSource(url="https://gitlab.com/owner/repo")
        assert repo.url == "https://gitlab.com/owner/repo"

    def test_repo_source_git_ssh_url(self):
        """RepoSource accepts git@ SSH URLs."""
        repo = RepoSource(url="git@github.com:owner/repo.git")
        assert repo.url == "git@github.com:owner/repo.git"

    def test_repo_source_invalid_url_rejected(self):
        """RepoSource rejects invalid URL formats."""
        import pydantic

        with pytest.raises(pydantic.ValidationError) as exc_info:
            RepoSource(url="not-a-valid-url")
        assert "URL must be 'owner/repo' format" in str(exc_info.value)

    def test_repo_source_missing_protocol_rejected(self):
        """RepoSource rejects URLs missing protocol."""
        import pydantic

        with pytest.raises(pydantic.ValidationError) as exc_info:
            RepoSource(url="github.com/owner/repo")
        assert "URL must be 'owner/repo' format" in str(exc_info.value)

    def test_repo_source_with_provider(self):
        """RepoSource accepts explicit provider specification."""
        repo = RepoSource(url="owner/repo", provider="gitlab")
        assert repo.url == "owner/repo"
        assert repo.provider == "gitlab"

    def test_repo_source_provider_options(self):
        """RepoSource accepts all valid provider options."""
        for provider in ["github", "gitlab", "bitbucket"]:
            repo = RepoSource(url="owner/repo", provider=provider)
            assert repo.provider == provider

    def test_repo_source_invalid_provider_rejected(self):
        """RepoSource rejects invalid provider values."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RepoSource(url="owner/repo", provider="invalid")

    def test_repo_source_provider_none_by_default(self):
        """RepoSource provider is None by default."""
        repo = RepoSource(url="owner/repo")
        assert repo.provider is None


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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Prompt Automation"
        assert data["prompt"] == test_prompt
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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

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
        assert automation.prompt == "Print hello"
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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_prompt_missing_prompt(self, async_client):
        """Missing prompt returns 422."""
        payload = {
            "name": "Test",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_prompt_empty_prompt(self, async_client):
        """Empty prompt returns 422."""
        payload = {
            "name": "Test",
            "prompt": "",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_prompt_invalid_cron(self, async_client):
        """Invalid cron schedule returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "invalid-cron"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_prompt_missing_trigger(self, async_client):
        """Missing trigger returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

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

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 500


# --- Plugin Preset Tests ---


class TestCreatePluginAutomationRequestValidation:
    """Tests for CreatePluginAutomationRequest validation."""

    def test_single_plugin_normalized_to_list(self):
        """Single PluginSource is normalized to a list."""
        from automation.preset_router import CreatePluginAutomationRequest

        request = CreatePluginAutomationRequest.model_validate(
            {
                "name": "Test",
                "plugins": {"source": "github:owner/repo", "ref": "main"},
                "prompt": "Test prompt",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            }
        )

        # Should be normalized to a list
        assert isinstance(request.plugins, list)
        assert len(request.plugins) == 1
        assert request.plugins[0].source == "github:owner/repo"
        assert request.plugins[0].ref == "main"

    def test_plugin_list_preserved(self):
        """List of plugins is preserved as-is."""
        from automation.preset_router import CreatePluginAutomationRequest

        request = CreatePluginAutomationRequest.model_validate(
            {
                "name": "Test",
                "plugins": [
                    {"source": "github:owner/repo1"},
                    {"source": "github:owner/repo2", "ref": "v1.0"},
                ],
                "prompt": "Test prompt",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            }
        )

        assert isinstance(request.plugins, list)
        assert len(request.plugins) == 2
        assert request.plugins[0].source == "github:owner/repo1"
        assert request.plugins[1].source == "github:owner/repo2"
        assert request.plugins[1].ref == "v1.0"

    def test_empty_plugin_list_rejected(self):
        """Empty plugin list raises validation error."""
        from automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="At least one plugin is required"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "plugins": [],
                    "prompt": "Test prompt",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )


class TestGeneratePluginTarball:
    """Tests for the plugin tarball generation function."""

    def test_generate_plugin_tarball_structure(self):
        """Generated plugin tarball contains expected files."""
        plugins = [PluginSource(source="github:owner/repo", ref="main")]
        prompt = "Run the plugin command"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        # Verify it's a valid tarball
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "main.py" in names
            assert "plugins_config.json" in names
            assert "prompt.txt" in names
            assert "setup.sh" in names

    def test_generate_plugin_tarball_plugins_config(self):
        """Generated tarball contains correct plugins_config.json."""
        plugins = [
            PluginSource(source="github:owner/repo1", ref="v1.0.0"),
            PluginSource(source="github:owner/repo2", repo_path="plugins/my-plugin"),
        ]
        prompt = "Test prompt"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            config_file = tar.extractfile("plugins_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode("utf-8"))

            assert len(config) == 2
            assert config[0]["source"] == "github:owner/repo1"
            assert config[0]["ref"] == "v1.0.0"
            assert config[1]["source"] == "github:owner/repo2"
            assert config[1]["repo_path"] == "plugins/my-plugin"

    def test_generate_plugin_tarball_prompt_content(self):
        """Generated tarball contains the user's prompt."""
        plugins = [PluginSource(source="github:owner/repo")]
        prompt = "/my-plugin:command --arg value"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            prompt_file = tar.extractfile("prompt.txt")
            assert prompt_file is not None
            prompt_content = prompt_file.read().decode("utf-8")
            assert prompt_content == prompt

    def test_generate_plugin_tarball_main_py_content(self):
        """Generated tarball contains valid main.py with plugin loading code."""
        plugins = [PluginSource(source="github:owner/repo")]
        prompt = "Test prompt"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            main_file = tar.extractfile("main.py")
            assert main_file is not None
            main_content = main_file.read().decode("utf-8")

            # Verify key SDK imports and patterns are present
            assert "from openhands.sdk import" in main_content
            assert "from openhands.sdk.plugin import PluginSource" in main_content
            assert "Conversation" in main_content
            assert "OpenHandsCloudWorkspace" in main_content
            assert "plugins_config.json" in main_content
            assert "PluginSource.model_validate" in main_content
            assert "plugins=plugin_sources" in main_content

    def test_generate_plugin_tarball_setup_sh_executable(self):
        """setup.sh in plugin tarball has executable permissions."""
        plugins = [PluginSource(source="github:owner/repo")]
        prompt = "Test prompt"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            setup_info = tar.getmember("setup.sh")
            # Check executable bit is set (0o755 includes 0o100 for owner execute)
            assert setup_info.mode & 0o100

    def test_generate_plugin_tarball_excludes_none_values(self):
        """Generated plugins_config.json excludes None values."""
        # ref and repo_path are None by default
        plugins = [PluginSource(source="github:owner/repo")]
        prompt = "Test prompt"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            config_file = tar.extractfile("plugins_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode("utf-8"))

            assert len(config) == 1
            assert config[0]["source"] == "github:owner/repo"
            # None values should be excluded
            assert "ref" not in config[0]
            assert "repo_path" not in config[0]

    def test_generate_plugin_tarball_without_repos(self):
        """Generated plugin tarball without repos does not include repos_config.json."""
        plugins = [PluginSource(source="github:owner/repo")]
        prompt = "Test prompt"
        tarball_bytes = _generate_plugin_tarball(plugins, prompt)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "repos_config.json" not in names

    def test_generate_plugin_tarball_with_repos(self):
        """Plugin tarball with repos includes repos config."""
        plugins = [PluginSource(source="github:owner/plugin")]
        prompt = "Test prompt"
        repos = [
            RepoSource(url="owner/repo1"),
            RepoSource(url="https://gitlab.com/owner/repo2", ref="develop"),
        ]
        tarball_bytes = _generate_plugin_tarball(plugins, prompt, repos=repos)

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "repos_config.json" in names
            # Note: clone_repos.py is no longer included - SDK handles cloning
            assert "clone_repos.py" not in names
            assert "plugins_config.json" in names  # All should be present

            # Verify repos config content
            repos_file = tar.extractfile("repos_config.json")
            assert repos_file is not None
            repos_config = json.load(repos_file)
            assert len(repos_config) == 2
            assert repos_config[0]["url"] == "owner/repo1"
            assert repos_config[1]["url"] == "https://gitlab.com/owner/repo2"
            assert repos_config[1]["ref"] == "develop"


@requires_docker
class TestCreateAutomationFromPlugin:
    """Tests for POST /v1/preset/plugin endpoint."""

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

    async def test_create_from_plugin_success(
        self, async_client, async_session, mock_file_store
    ):
        """Valid request creates automation and upload, returns 201."""
        payload = {
            "name": "My Plugin Automation",
            "plugins": [
                {"source": "github:owner/code-review-plugin", "ref": "v1.0.0"},
                {"source": "github:owner/security-plugin"},
            ],
            "prompt": "Review all Python files for security issues",
            "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Plugin Automation"
        assert data["prompt"] == "Review all Python files for security issues"
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
            assert "plugins_config.json" in tar.getnames()
            assert "prompt.txt" in tar.getnames()
            assert "setup.sh" in tar.getnames()

            # Verify plugins config
            config_file = tar.extractfile("plugins_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode())
            assert len(config) == 2
            assert config[0]["source"] == "github:owner/code-review-plugin"
            assert config[0]["ref"] == "v1.0.0"
            assert config[1]["source"] == "github:owner/security-plugin"

    async def test_create_from_plugin_creates_upload_record(
        self, async_client, async_session, mock_file_store
    ):
        """Endpoint creates a TarballUpload record."""
        payload = {
            "name": "Upload Test",
            "plugins": [{"source": "github:owner/plugin"}],
            "prompt": "Do something with plugin",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

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
        assert "plugin-automation" in upload.name

    async def test_create_from_plugin_creates_automation_record(
        self, async_client, async_session, mock_file_store
    ):
        """Endpoint creates an Automation record."""
        payload = {
            "name": "Automation Record Test",
            "plugins": [{"source": "github:owner/plugin", "ref": "main"}],
            "prompt": "Run plugin tasks",
            "trigger": {"type": "cron", "schedule": "30 10 * * 5"},
            "timeout": 300,
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

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
        assert automation.prompt == "Run plugin tasks"
        assert automation.entrypoint == "python main.py"
        assert automation.setup_script_path == "setup.sh"
        assert automation.timeout == 300
        assert automation.user_id == TEST_USER_ID
        assert automation.org_id == TEST_ORG_ID

    async def test_create_from_plugin_missing_plugins(self, async_client):
        """Missing plugins returns 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_plugin_empty_plugins(self, async_client):
        """Empty plugins list returns 422."""
        payload = {
            "name": "Test",
            "plugins": [],
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_plugin_missing_prompt(self, async_client):
        """Missing prompt returns 422."""
        payload = {
            "name": "Test",
            "plugins": [{"source": "github:owner/plugin"}],
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_plugin_invalid_cron(self, async_client):
        """Invalid cron schedule returns 422."""
        payload = {
            "name": "Test",
            "plugins": [{"source": "github:owner/plugin"}],
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "invalid-cron"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 422

    async def test_create_from_plugin_with_repo_path(
        self, async_client, async_session, mock_file_store
    ):
        """Plugin with repo_path for monorepo is properly serialized."""
        payload = {
            "name": "Monorepo Plugin Test",
            "plugins": [
                {
                    "source": "github:company/monorepo",
                    "ref": "main",
                    "repo_path": "plugins/my-plugin",
                }
            ],
            "prompt": "Use the monorepo plugin",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 201

        # Verify tarball contains correct config
        tarball_bytes = mock_file_store._captured_content
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            config_file = tar.extractfile("plugins_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode())
            assert config[0]["source"] == "github:company/monorepo"
            assert config[0]["ref"] == "main"
            assert config[0]["repo_path"] == "plugins/my-plugin"

    async def test_create_from_plugin_storage_failure(
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
            "plugins": [{"source": "github:owner/plugin"}],
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 500

    async def test_create_from_plugin_single_plugin_object(
        self, async_client, async_session, mock_file_store
    ):
        """Single plugin object (not in list) is accepted."""
        payload = {
            "name": "Single Plugin Test",
            "plugins": {"source": "github:owner/single-plugin", "ref": "v2.0.0"},
            "prompt": "Use single plugin",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Single Plugin Test"

        # Verify tarball contains the plugin as a list
        tarball_bytes = mock_file_store._captured_content
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            config_file = tar.extractfile("plugins_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode())
            # Should be normalized to a list
            assert isinstance(config, list)
            assert len(config) == 1
            assert config[0]["source"] == "github:owner/single-plugin"
            assert config[0]["ref"] == "v2.0.0"

    async def test_create_from_plugin_single_plugin_minimal(
        self, async_client, async_session, mock_file_store
    ):
        """Single plugin with only source is accepted."""
        payload = {
            "name": "Minimal Plugin Test",
            "plugins": {"source": "github:owner/minimal-plugin"},
            "prompt": "Use plugin",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 201

        # Verify tarball
        tarball_bytes = mock_file_store._captured_content
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            config_file = tar.extractfile("plugins_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode())
            assert len(config) == 1
            assert config[0]["source"] == "github:owner/minimal-plugin"
            # No ref or repo_path since they were None
            assert "ref" not in config[0]
