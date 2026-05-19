"""Tests for preset-based automation creation endpoint."""

import io
import json
import socket
import tarfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openhands.automation.models import Automation, TarballUpload, UploadStatus
from openhands.automation.preset_router import (
    _generate_plugin_tarball,
    _generate_tarball,
)
from openhands.sdk.plugin import PluginSource
from openhands.workspace import RepoSource


# Test UUIDs matching mock_authenticated_user fixture
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")

# Path to preset files
PRESETS_DIR = Path(__file__).parent.parent / "openhands" / "automation" / "presets"


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

    def test_prompt_setup_sh_fetches_sdk_version_from_api(self):
        """Prompt setup.sh fetches SDK version from the automation service API."""
        setup_sh_path = PRESETS_DIR / "prompt" / "setup.sh"
        content = setup_sh_path.read_text()
        assert "${AUTOMATION_API_URL}/sdk-version" in content, (
            "setup.sh must call ${AUTOMATION_API_URL}/sdk-version "
            "— do not hardcode the version"
        )

    def test_plugin_setup_sh_fetches_sdk_version_from_api(self):
        """Plugin setup.sh fetches SDK version from the automation service API."""
        setup_sh_path = PRESETS_DIR / "plugin" / "setup.sh"
        content = setup_sh_path.read_text()
        assert "${AUTOMATION_API_URL}/sdk-version" in content, (
            "setup.sh must call ${AUTOMATION_API_URL}/sdk-version "
            "— do not hardcode the version"
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
            assert "RemoteWorkspace" in main_content
            assert "workspace.get_llm()" in main_content
            assert "workspace.get_secrets()" in main_content
            assert "workspace.get_mcp_config()" in main_content
            assert "workspace.clone_repos" in main_content
            assert "workspace.load_skills_from_agent_server" in main_content
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
            RepoSource(url="owner/repo1", provider="github"),
            RepoSource(url="owner/repo2", ref="main", provider="github"),
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
            assert repos_config[0]["provider"] == "github"
            assert "ref" not in repos_config[0]  # None excluded
            assert repos_config[1]["url"] == "owner/repo2"
            assert repos_config[1]["ref"] == "main"


class TestRepoSource:
    """Tests for RepoSource model."""

    # --- Short URL format (requires provider) ---

    def test_repo_source_short_url_with_provider(self):
        """RepoSource accepts short URL with explicit provider."""
        repo = RepoSource(url="owner/repo", provider="github")
        assert repo.url == "owner/repo"
        assert repo.provider == "github"

    def test_repo_source_short_url_with_ref_and_provider(self):
        """RepoSource accepts short URL with ref and provider."""
        repo = RepoSource(url="owner/repo", ref="v1.0.0", provider="github")
        assert repo.url == "owner/repo"
        assert repo.ref == "v1.0.0"

    def test_repo_source_short_url_without_provider_rejected(self):
        """RepoSource rejects short URL without provider."""
        import pydantic

        with pytest.raises(pydantic.ValidationError) as exc_info:
            RepoSource(url="owner/repo")
        assert "requires explicit 'provider' field" in str(exc_info.value)

    def test_repo_source_string_without_provider_rejected(self):
        """RepoSource rejects string input without provider."""
        import pydantic

        with pytest.raises(pydantic.ValidationError) as exc_info:
            RepoSource.model_validate("owner/repo")
        assert "requires explicit 'provider' field" in str(exc_info.value)

    # --- Full URL format (provider auto-detected) ---

    def test_repo_source_full_url_github(self):
        """RepoSource auto-detects GitHub from full URL."""
        repo = RepoSource(url="https://github.com/owner/repo")
        assert repo.url == "https://github.com/owner/repo"
        assert repo.provider is None  # Auto-detected, not stored

    def test_repo_source_full_url_gitlab(self):
        """RepoSource auto-detects GitLab from full URL."""
        repo = RepoSource(url="https://gitlab.com/owner/repo")
        assert repo.url == "https://gitlab.com/owner/repo"

    def test_repo_source_full_url_bitbucket(self):
        """RepoSource auto-detects Bitbucket from full URL."""
        repo = RepoSource(url="https://bitbucket.org/owner/repo")
        assert repo.url == "https://bitbucket.org/owner/repo"

    def test_repo_source_git_ssh_url(self):
        """RepoSource accepts git@ SSH URLs (provider auto-detected)."""
        repo = RepoSource(url="git@github.com:owner/repo.git")
        assert repo.url == "git@github.com:owner/repo.git"

    # --- Provider options ---

    def test_repo_source_provider_github(self):
        """RepoSource accepts github provider."""
        repo = RepoSource(url="owner/repo", provider="github")
        assert repo.provider == "github"

    def test_repo_source_provider_gitlab(self):
        """RepoSource accepts gitlab provider."""
        repo = RepoSource(url="owner/repo", provider="gitlab")
        assert repo.provider == "gitlab"

    def test_repo_source_provider_bitbucket(self):
        """RepoSource accepts bitbucket provider."""
        repo = RepoSource(url="owner/repo", provider="bitbucket")
        assert repo.provider == "bitbucket"

    def test_repo_source_invalid_provider_rejected(self):
        """RepoSource rejects invalid provider values."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RepoSource(url="owner/repo", provider="invalid")  # type: ignore[arg-type]

    # --- URL validation ---

    def test_repo_source_invalid_url_rejected(self):
        """RepoSource rejects invalid URL formats."""
        import pydantic

        with pytest.raises(pydantic.ValidationError) as exc_info:
            RepoSource(url="not-a-valid-url", provider="github")
        assert "URL must be 'owner/repo' format" in str(exc_info.value)

    def test_repo_source_missing_protocol_rejected(self):
        """RepoSource rejects URLs missing protocol."""
        import pydantic

        with pytest.raises(pydantic.ValidationError) as exc_info:
            RepoSource(url="github.com/owner/repo", provider="github")
        assert "URL must be 'owner/repo' format" in str(exc_info.value)


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
        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

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
        assert data["entrypoint"] == ".venv/bin/python main.py"
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
        assert automation.entrypoint == ".venv/bin/python main.py"
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
        from openhands.automation.preset_router import CreatePluginAutomationRequest

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
        from openhands.automation.preset_router import CreatePluginAutomationRequest

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
        from openhands.automation.preset_router import CreatePluginAutomationRequest

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
            assert "RemoteWorkspace" in main_content
            assert "workspace.get_llm()" in main_content
            assert "workspace.get_secrets()" in main_content
            assert "workspace.clone_repos" in main_content
            assert "workspace.load_skills_from_agent_server" in main_content
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
            RepoSource(url="owner/repo1", provider="github"),
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
            assert repos_config[0]["provider"] == "github"
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
        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

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
        assert data["entrypoint"] == ".venv/bin/python main.py"
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
        assert automation.entrypoint == ".venv/bin/python main.py"
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


@requires_docker
class TestRegeneratePresetAutomation:
    """Tests for POST /v1/preset/{automation_id}/regenerate endpoint."""

    @pytest.fixture
    def mock_file_store(self):
        """Create a stateful mock file store that supports read and write."""
        from collections.abc import AsyncIterator
        from unittest.mock import AsyncMock

        store = MagicMock()
        store._storage: dict[str, bytes] = {}

        async def mock_write_stream(
            path: str,
            stream: AsyncIterator[bytes],
            max_size: int | None = None,
            content_type: str = "application/octet-stream",
        ) -> int:
            content = b""
            async for chunk in stream:
                content += chunk
            store._storage[path] = content
            return len(content)

        def mock_read(path: str) -> bytes:
            if path not in store._storage:
                raise FileNotFoundError(f"File not found: {path}")
            return store._storage[path]

        store.write_stream = AsyncMock(side_effect=mock_write_stream)
        store.read = MagicMock(side_effect=mock_read)
        store.delete = MagicMock()
        return store

    @pytest.fixture(autouse=True)
    def setup_file_store_override(self, mock_file_store):
        """Override file_store for all tests in this class."""
        from openhands.automation.app import app
        from openhands.automation.storage import get_file_store

        app.dependency_overrides[get_file_store] = lambda: mock_file_store
        yield
        app.dependency_overrides.pop(get_file_store, None)

    def _make_legacy_tarball(
        self,
        prompt: str,
        repos_config: list | None = None,
    ) -> bytes:
        """Build a minimal legacy tarball with old template code."""
        buf = io.BytesIO()
        old_main = (
            "from openhands.tools import get_default_agent\n"
            "print('hello')\n"
        )
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in [
                ("main.py", old_main.encode()),
                ("prompt.txt", prompt.encode()),
                ("setup.sh", b"#!/bin/bash\npip install openhands-sdk\n"),
            ]:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

            if repos_config is not None:
                rc = json.dumps(repos_config).encode()
                info = tarfile.TarInfo(name="repos_config.json")
                info.size = len(rc)
                tar.addfile(info, io.BytesIO(rc))

        buf.seek(0)
        return buf.read()

    async def test_regenerate_with_stored_prompt(
        self, async_client, async_session, mock_file_store
    ):
        """Regeneration uses the stored DB prompt and replaces the tarball."""
        # 1. Create an automation via the preset endpoint (stores prompt in DB).
        create_resp = await async_client.post(
            "/api/automation/v1/preset/prompt",
            json={
                "name": "Echo Bot",
                "prompt": "Reply hello echo world to every GitHub issue",
                "trigger": {
                    "type": "event",
                    "source": "github",
                    "on": "issues.labeled",
                    "filter": "label.name == 'openhands-test'",
                },
            },
        )
        assert create_resp.status_code == 201
        automation_id = create_resp.json()["id"]
        original_tarball_path = create_resp.json()["tarball_path"]

        # 2. Regenerate.
        regen_resp = await async_client.post(
            f"/api/automation/v1/preset/{automation_id}/regenerate"
        )
        assert regen_resp.status_code == 200
        data = regen_resp.json()

        # tarball_path must have changed.
        assert data["tarball_path"] != original_tarball_path
        assert data["tarball_path"].startswith("oh-internal://uploads/")

        # 3. The new tarball must contain the current template (not the old import).
        new_tarball_path = data["tarball_path"]
        new_upload_id = new_tarball_path.removeprefix("oh-internal://uploads/")
        storage_path = f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/{new_upload_id}.tar"
        new_tarball_bytes = mock_file_store._storage[storage_path]

        with tarfile.open(fileobj=io.BytesIO(new_tarball_bytes), mode="r:gz") as tar:
            main_file = tar.extractfile("main.py")
            assert main_file is not None
            main_content = main_file.read().decode()
            # New template uses the scoped import, not the old bare import.
            assert "from openhands.tools.preset.default import get_default_agent" in main_content
            assert "from openhands.tools import get_default_agent" not in main_content

            prompt_file = tar.extractfile("prompt.txt")
            assert prompt_file is not None
            assert prompt_file.read().decode() == "Reply hello echo world to every GitHub issue"

    async def test_regenerate_legacy_automation_null_prompt(
        self, async_client, async_session, mock_file_store
    ):
        """Legacy automation (prompt=None in DB) is regenerated from tarball."""
        # Build a legacy tarball and store it directly in the mock file store.
        legacy_prompt = "Reply hello echo world to every GitHub issue"
        legacy_tarball = self._make_legacy_tarball(legacy_prompt)

        legacy_upload_id = uuid.uuid4()
        legacy_storage_path = (
            f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/{legacy_upload_id}.tar"
        )
        mock_file_store._storage[legacy_storage_path] = legacy_tarball

        # Insert matching TarballUpload and Automation records directly.
        from openhands.automation.models import TarballUpload, UploadStatus, Automation

        upload = TarballUpload(
            id=legacy_upload_id,
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="legacy-upload",
            status=UploadStatus.COMPLETED,
            storage_path=legacy_storage_path,
            size_bytes=len(legacy_tarball),
        )
        async_session.add(upload)

        legacy_automation_id = uuid.uuid4()
        automation = Automation(
            id=legacy_automation_id,
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Legacy Echo Bot",
            prompt=None,  # Simulates pre-prompt-field automation.
            trigger={"type": "event", "source": "github", "on": "issues.labeled"},
            tarball_path=f"oh-internal://uploads/{legacy_upload_id}",
            setup_script_path="setup.sh",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.flush()

        # Regenerate.
        regen_resp = await async_client.post(
            f"/api/automation/v1/preset/{legacy_automation_id}/regenerate"
        )
        assert regen_resp.status_code == 200
        data = regen_resp.json()

        # tarball_path must have changed to the new upload.
        assert data["tarball_path"] != f"oh-internal://uploads/{legacy_upload_id}"
        assert data["tarball_path"].startswith("oh-internal://uploads/")

        # Prompt must be backfilled in the response.
        assert data["prompt"] == legacy_prompt

        # New tarball contains current template.
        new_tarball_path = data["tarball_path"]
        new_upload_id = new_tarball_path.removeprefix("oh-internal://uploads/")
        storage_path = f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/{new_upload_id}.tar"
        new_tarball_bytes = mock_file_store._storage[storage_path]

        with tarfile.open(fileobj=io.BytesIO(new_tarball_bytes), mode="r:gz") as tar:
            main_file = tar.extractfile("main.py")
            assert main_file is not None
            main_content = main_file.read().decode()
            assert "from openhands.tools.preset.default import get_default_agent" in main_content

            prompt_file = tar.extractfile("prompt.txt")
            assert prompt_file is not None
            assert prompt_file.read().decode() == legacy_prompt

    async def test_regenerate_preserves_repos(
        self, async_client, async_session, mock_file_store
    ):
        """Repos config in the original tarball is preserved after regeneration."""
        repos_config = [{"url": "owner/my-repo", "provider": "github", "ref": "main"}]
        legacy_prompt = "Analyze the repo"
        legacy_tarball = self._make_legacy_tarball(legacy_prompt, repos_config=repos_config)

        legacy_upload_id = uuid.uuid4()
        legacy_storage_path = (
            f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/{legacy_upload_id}.tar"
        )
        mock_file_store._storage[legacy_storage_path] = legacy_tarball

        from openhands.automation.models import TarballUpload, UploadStatus, Automation

        upload = TarballUpload(
            id=legacy_upload_id,
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="legacy-repos-upload",
            status=UploadStatus.COMPLETED,
            storage_path=legacy_storage_path,
            size_bytes=len(legacy_tarball),
        )
        async_session.add(upload)

        legacy_automation_id = uuid.uuid4()
        automation = Automation(
            id=legacy_automation_id,
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Repo Analyzer",
            prompt=None,
            trigger={"type": "cron", "schedule": "0 9 * * 1"},
            tarball_path=f"oh-internal://uploads/{legacy_upload_id}",
            setup_script_path="setup.sh",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.flush()

        regen_resp = await async_client.post(
            f"/api/automation/v1/preset/{legacy_automation_id}/regenerate"
        )
        assert regen_resp.status_code == 200

        new_tarball_path = regen_resp.json()["tarball_path"]
        new_upload_id = new_tarball_path.removeprefix("oh-internal://uploads/")
        storage_path = f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/{new_upload_id}.tar"
        new_tarball_bytes = mock_file_store._storage[storage_path]

        with tarfile.open(fileobj=io.BytesIO(new_tarball_bytes), mode="r:gz") as tar:
            rc_file = tar.extractfile("repos_config.json")
            assert rc_file is not None
            restored = json.loads(rc_file.read().decode())
            assert restored == repos_config

    async def test_regenerate_automation_not_found(self, async_client):
        """Returns 404 for an unknown automation ID."""
        missing_id = uuid.uuid4()
        response = await async_client.post(
            f"/api/automation/v1/preset/{missing_id}/regenerate"
        )
        assert response.status_code == 404

    async def test_regenerate_no_prompt_no_tarball_prompt(
        self, async_client, async_session, mock_file_store
    ):
        """Returns 422 when automation has no stored prompt and no prompt.txt."""
        # Build a tarball without prompt.txt.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            content = b"#!/bin/bash\necho hi\n"
            info = tarfile.TarInfo(name="setup.sh")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buf.seek(0)
        no_prompt_tarball = buf.read()

        upload_id = uuid.uuid4()
        storage_path = f"uploads/{TEST_ORG_ID}/{TEST_USER_ID}/{upload_id}.tar"
        mock_file_store._storage[storage_path] = no_prompt_tarball

        from openhands.automation.models import TarballUpload, UploadStatus, Automation

        upload = TarballUpload(
            id=upload_id,
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="no-prompt-upload",
            status=UploadStatus.COMPLETED,
            storage_path=storage_path,
            size_bytes=len(no_prompt_tarball),
        )
        async_session.add(upload)

        automation_id = uuid.uuid4()
        automation = Automation(
            id=automation_id,
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Non-Preset Automation",
            prompt=None,
            trigger={"type": "cron", "schedule": "0 0 * * *"},
            tarball_path=f"oh-internal://uploads/{upload_id}",
            setup_script_path="setup.sh",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.flush()

        response = await async_client.post(
            f"/api/automation/v1/preset/{automation_id}/regenerate"
        )
        assert response.status_code == 422
        assert "no prompt found" in response.json()["detail"]

