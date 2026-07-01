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
    _get_preset_entrypoint,
    _replace_prompt_in_tarball,
    _resolve_experiment_variant_models,
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


class TestPresetEntrypoint:
    def test_get_preset_entrypoint_posix(self, monkeypatch):
        monkeypatch.setattr("openhands.automation.preset_router.os.name", "posix")
        assert _get_preset_entrypoint() == ".venv/bin/python main.py"

    def test_get_preset_entrypoint_windows(self, monkeypatch):
        monkeypatch.setattr("openhands.automation.preset_router.os.name", "nt")
        assert _get_preset_entrypoint() == ".venv/Scripts/python.exe main.py"

    def test_prompt_setup_sh_falls_back_when_python3_missing(self):
        setup_sh_path = PRESETS_DIR / "prompt" / "setup.sh"
        content = setup_sh_path.read_text()
        assert "command -v python3" in content
        assert "command -v python" in content
        assert "command -v py" in content

    def test_plugin_setup_sh_falls_back_when_python3_missing(self):
        setup_sh_path = PRESETS_DIR / "plugin" / "setup.sh"
        content = setup_sh_path.read_text()
        assert "command -v python3" in content
        assert "command -v python" in content
        assert "command -v py" in content


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
            assert "keep_alive=True" in main_content
            assert "RemoteWorkspace" in main_content
            assert "workspace.get_llm(profile_name=model_profile)" in main_content
            assert "falling back to active/default profile" in main_content
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


class TestReplacePromptInTarball:
    """Tests for swapping prompt.txt inside an existing preset tarball."""

    def test_replaces_prompt_and_preserves_sibling_files(self):
        """The prompt is swapped while every other file is left byte-for-byte intact."""
        # Arrange — a plugin preset tarball carries main.py, setup.sh, prompt.txt,
        # plugins_config.json and repos_config.json; all but the prompt must survive.
        original = _generate_plugin_tarball(
            [PluginSource(source="github:owner/repo")],
            "Original prompt",
            repos=[RepoSource(url="owner/repo", provider="github")],
        )

        # Act
        updated = _replace_prompt_in_tarball(original, "New prompt")

        # Assert
        assert updated is not None

        def _read(tarball_bytes):
            files = {}
            with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    files[member.name] = extracted.read()
                return files, tar.getmember("setup.sh").mode

        old_files, _ = _read(original)
        new_files, new_setup_mode = _read(updated)

        assert new_files["prompt.txt"].decode() == "New prompt"
        for name in ("main.py", "setup.sh", "plugins_config.json", "repos_config.json"):
            assert new_files[name] == old_files[name]
        assert new_setup_mode & 0o100  # setup.sh stays executable

    def test_returns_none_when_tarball_has_no_prompt(self):
        """A tarball without prompt.txt is not regenerable, so None is returned."""
        # Arrange — an archive that has no prompt.txt member.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            data = b"print('hi')"
            info = tarfile.TarInfo(name="main.py")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        # Act / Assert
        assert _replace_prompt_in_tarball(buffer.getvalue(), "New prompt") is None


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
            "model": "fast-profile",
            "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Prompt Automation"
        assert data["model"] == "fast-profile"

        assert data["prompt"] == test_prompt
        assert data["trigger"]["type"] == "cron"
        assert data["trigger"]["schedule"] == "0 9 * * 1"
        assert data["entrypoint"] == _get_preset_entrypoint()
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
            assert "automation_model.py" not in tar.getnames()

            # Verify prompt content matches what was sent
            prompt_file = tar.extractfile("prompt.txt")
            assert prompt_file is not None
            assert prompt_file.read().decode() == test_prompt

    async def test_create_from_prompt_defaults_to_active_model_profile(
        self, async_client, mock_authenticated_user
    ):
        """Prompt preset stores the active profile name when none is requested."""
        mock_authenticated_user.model_profile_names = frozenset({"active-profile"})
        mock_authenticated_user.active_model_profile_name = "active-profile"

        response = await async_client.post(
            "/api/automation/v1/preset/prompt",
            json={
                "name": "My Prompt Automation",
                "prompt": "Do something",
                "trigger": {"type": "cron", "schedule": "0 9 * * 1"},
            },
        )

        assert response.status_code == 201
        assert response.json()["model"] == "active-profile"

    async def test_create_from_prompt_unknown_model_profile_rejected(
        self, async_client, mock_file_store, mock_authenticated_user
    ):
        """Prompt preset rejects unknown profiles when auth metadata includes names."""
        mock_authenticated_user.model_profile_names = frozenset({"allowed-profile"})
        response = await async_client.post(
            "/api/automation/v1/preset/prompt",
            json={
                "name": "My Prompt Automation",
                "prompt": "Do something",
                "model": "missing-profile",
                "trigger": {"type": "cron", "schedule": "0 9 * * 1"},
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "Model profile `missing-profile` not found"
        mock_file_store.write_stream.assert_not_called()

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
        assert automation.entrypoint == _get_preset_entrypoint()
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

    async def test_create_from_prompt_impossible_cron(self, async_client):
        """Cron schedules that can never fire return 422."""
        payload = {
            "name": "Test",
            "prompt": "Do something",
            "trigger": {"type": "cron", "schedule": "0 0 31 2 *"},
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

    async def test_create_from_prompt_without_timeout_defaults(
        self, async_client, mock_file_store
    ):
        """Prompt preset stores the configured default timeout when omitted."""
        payload = {
            "name": "Default Timeout Test",
            "prompt": "Short task",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 201
        assert response.json()["timeout"] == 600

    async def test_create_from_prompt_timeout_exceeds_max_rejected(self, async_client):
        """Prompt preset rejects timeouts over 30 minutes."""
        payload = {
            "name": "Timeout Test",
            "prompt": "Long running task",
            "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            "timeout": 1801,
        }

        response = await async_client.post(
            "/api/automation/v1/preset/prompt", json=payload
        )

        assert response.status_code == 422

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
            assert "keep_alive=True" in main_content
            assert "RemoteWorkspace" in main_content
            assert "workspace.get_llm(profile_name=model_profile)" in main_content
            assert "falling back to active/default profile" in main_content
            assert "workspace.get_secrets()" in main_content
            assert "workspace.clone_repos" in main_content
            assert "workspace.load_skills_from_agent_server" in main_content
            assert "plugins_config.json" in main_content
            assert "PluginSource.model_validate" in main_content
            assert '"plugins": plugin_sources' in main_content
            assert "Conversation(**conversation_kwargs)" in main_content

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
            assert "automation_model.py" not in names

            # Verify repos config content
            repos_file = tar.extractfile("repos_config.json")
            assert repos_file is not None
            repos_config = json.load(repos_file)
            assert len(repos_config) == 2
            assert repos_config[0]["url"] == "owner/repo1"
            assert repos_config[0]["provider"] == "github"
            assert repos_config[1]["url"] == "https://gitlab.com/owner/repo2"
            assert repos_config[1]["ref"] == "develop"


class TestExperimentVariantValidation:
    """Tests for A/B test variant validation on CreatePluginAutomationRequest."""

    def test_variants_accepted(self):
        """Valid variants request is accepted."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        request = CreatePluginAutomationRequest.model_validate(
            {
                "name": "AB Test",
                "experiment_id": "my-experiment",
                "variants": [
                    {
                        "name": "control",
                        "weight": 50,
                        "model": "fast-profile",
                        "plugins": [{"source": "github:owner/repo", "ref": "v1"}],
                    },
                    {
                        "name": "treatment",
                        "weight": 50,
                        "plugins": [{"source": "github:owner/repo", "ref": "v2"}],
                    },
                ],
                "prompt": "Test prompt",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            }
        )
        assert request.variants is not None
        assert len(request.variants) == 2
        assert request.variants[0].model == "fast-profile"
        assert request.variants[1].model is None

        assert request.plugins is None
        assert request.experiment_id == "my-experiment"

    def test_variant_models_default_to_active_profile(self, mock_authenticated_user):
        """Missing variant model profiles resolve to the active profile."""
        from openhands.automation.preset_router import ExperimentVariant

        mock_authenticated_user.model_profile_names = frozenset(
            {"active-profile", "fast-profile"}
        )
        mock_authenticated_user.active_model_profile_name = "active-profile"
        variants = [
            ExperimentVariant(
                name="control",
                weight=50,
                model="fast-profile",
                plugins=[PluginSource(source="github:owner/repo", ref="v1")],
            ),
            ExperimentVariant(
                name="treatment",
                weight=50,
                plugins=[PluginSource(source="github:owner/repo", ref="v2")],
            ),
        ]

        resolved = _resolve_experiment_variant_models(variants, mock_authenticated_user)

        assert resolved is not None
        assert resolved[0].model == "fast-profile"
        assert resolved[1].model == "active-profile"

    def test_unknown_variant_model_profile_rejected(self, mock_authenticated_user):
        """Variant model profile names are validated when metadata is available."""
        from fastapi import HTTPException

        from openhands.automation.preset_router import ExperimentVariant

        mock_authenticated_user.model_profile_names = frozenset({"active-profile"})
        mock_authenticated_user.active_model_profile_name = "active-profile"
        variants = [
            ExperimentVariant(
                name="control",
                weight=50,
                model="missing-profile",
                plugins=[PluginSource(source="github:owner/repo", ref="v1")],
            ),
            ExperimentVariant(
                name="treatment",
                weight=50,
                plugins=[PluginSource(source="github:owner/repo", ref="v2")],
            ),
        ]

        with pytest.raises(HTTPException) as exc_info:
            _resolve_experiment_variant_models(variants, mock_authenticated_user)
        assert exc_info.value.detail == "Model profile `missing-profile` not found"

    def test_plugins_and_variants_mutually_exclusive(self):
        """Providing both plugins and variants raises error."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="Exactly one of"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Bad",
                    "plugins": [{"source": "github:owner/repo"}],
                    "variants": [
                        {
                            "name": "a",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                        {
                            "name": "b",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                    ],
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_neither_plugins_nor_variants_rejected(self):
        """Providing neither plugins nor variants raises error."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="Exactly one of"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Bad",
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_experiment_id_required_with_variants(self):
        """experiment_id is required when variants is used."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="experiment_id.*required"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "variants": [
                        {
                            "name": "a",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                        {
                            "name": "b",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                    ],
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_experiment_id_rejected_with_plugins(self):
        """experiment_id cannot be used with plugins (only variants)."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="experiment_id.*can only be used with"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "plugins": [{"source": "github:owner/repo"}],
                    "experiment_id": "oops",
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_single_variant_rejected(self):
        """At least two variants are required."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="At least two variants"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "experiment_id": "test",
                    "variants": [
                        {
                            "name": "only",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                    ],
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_duplicate_variant_names_rejected(self):
        """Variant names must be unique."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError, match="unique"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "experiment_id": "test",
                    "variants": [
                        {
                            "name": "same",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                        {
                            "name": "same",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                    ],
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_zero_weight_rejected(self):
        """Variant weight must be positive."""
        from openhands.automation.preset_router import CreatePluginAutomationRequest

        with pytest.raises(ValueError):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "experiment_id": "test",
                    "variants": [
                        {
                            "name": "a",
                            "weight": 0,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                        {
                            "name": "b",
                            "weight": 1,
                            "plugins": [{"source": "github:owner/repo"}],
                        },
                    ],
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )

    def test_max_variants_accepted(self):
        """Exactly MAX_VARIANTS variants is accepted."""
        from openhands.automation.preset_router import (
            MAX_VARIANTS,
            CreatePluginAutomationRequest,
        )

        variants = [
            {"name": f"v{i}", "weight": 1, "plugins": [{"source": "github:owner/repo"}]}
            for i in range(MAX_VARIANTS)
        ]
        req = CreatePluginAutomationRequest.model_validate(
            {
                "name": "Test",
                "experiment_id": "boundary-test",
                "variants": variants,
                "prompt": "Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            }
        )
        assert req.variants is not None and len(req.variants) == MAX_VARIANTS

    def test_too_many_variants_rejected(self):
        """More than MAX_VARIANTS is rejected."""
        from openhands.automation.preset_router import (
            MAX_VARIANTS,
            CreatePluginAutomationRequest,
        )

        variants = [
            {"name": f"v{i}", "weight": 1, "plugins": [{"source": "github:owner/repo"}]}
            for i in range(MAX_VARIANTS + 1)
        ]
        with pytest.raises(ValueError, match="At most"):
            CreatePluginAutomationRequest.model_validate(
                {
                    "name": "Test",
                    "experiment_id": "test",
                    "variants": variants,
                    "prompt": "Test",
                    "trigger": {"type": "cron", "schedule": "0 0 * * *"},
                }
            )


class TestExperimentTarball:
    """Tests for experiment (A/B) tarball generation."""

    def test_experiment_tarball_contains_experiment_config(self):
        """Experiment tarball has experiment_config.json, not plugins_config.json."""
        from openhands.automation.preset_router import ExperimentVariant

        variants = [
            ExperimentVariant(
                name="control",
                weight=50,
                plugins=[PluginSource(source="github:owner/repo", ref="v1")],
            ),
            ExperimentVariant(
                name="treatment",
                weight=50,
                plugins=[PluginSource(source="github:owner/repo", ref="v2")],
            ),
        ]
        tarball_bytes = _generate_plugin_tarball(
            None,
            "Test prompt",
            experiment_id="test-exp",
            variants=variants,
        )

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "experiment_config.json" in names
            assert "plugins_config.json" not in names
            assert "main.py" in names
            assert "prompt.txt" in names
            assert "setup.sh" in names

    def test_experiment_config_content(self):
        """experiment_config.json has correct structure."""
        from openhands.automation.preset_router import ExperimentVariant

        variants = [
            ExperimentVariant(
                name="control",
                weight=70,
                model="fast-profile",
                plugins=[PluginSource(source="github:owner/repo", ref="v1")],
            ),
            ExperimentVariant(
                name="treatment",
                weight=30,
                model="reasoning-profile",
                plugins=[PluginSource(source="github:owner/repo", ref="v2")],
            ),
        ]
        tarball_bytes = _generate_plugin_tarball(
            None,
            "Test prompt",
            experiment_id="my-exp",
            variants=variants,
        )

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            config_file = tar.extractfile("experiment_config.json")
            assert config_file is not None
            config = json.loads(config_file.read().decode("utf-8"))

            assert config["experiment_id"] == "my-exp"
            assert len(config["variants"]) == 2
            assert config["variants"][0]["name"] == "control"
            assert config["variants"][0]["weight"] == 70
            assert config["variants"][0]["plugins"][0]["source"] == "github:owner/repo"
            assert config["variants"][0]["plugins"][0]["ref"] == "v1"
            assert config["variants"][0]["model"] == "fast-profile"

            assert config["variants"][1]["name"] == "treatment"
            assert config["variants"][1]["weight"] == 30
            assert config["variants"][1]["model"] == "reasoning-profile"

    def test_standard_tarball_unchanged(self):
        """Non-experiment tarball still produces plugins_config.json."""
        plugins = [PluginSource(source="github:owner/repo", ref="main")]
        tarball_bytes = _generate_plugin_tarball(plugins, "Test prompt")

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            assert "plugins_config.json" in names
            assert "experiment_config.json" not in names


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
        assert data["entrypoint"] == _get_preset_entrypoint()
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

    async def test_create_from_plugin_defaults_to_active_model_profile(
        self, async_client, mock_authenticated_user
    ):
        """Plugin preset stores the active profile name when none is requested."""
        mock_authenticated_user.model_profile_names = frozenset({"active-profile"})
        mock_authenticated_user.active_model_profile_name = "active-profile"

        response = await async_client.post(
            "/api/automation/v1/preset/plugin",
            json={
                "name": "My Plugin Automation",
                "plugins": [{"source": "github:owner/plugin"}],
                "prompt": "Do something",
                "trigger": {"type": "cron", "schedule": "0 9 * * 1"},
            },
        )

        assert response.status_code == 201
        assert response.json()["model"] == "active-profile"

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
        assert automation.entrypoint == _get_preset_entrypoint()
        assert automation.setup_script_path == "setup.sh"
        assert automation.timeout == 300
        assert automation.user_id == TEST_USER_ID
        assert automation.org_id == TEST_ORG_ID

    async def test_create_from_plugin_without_timeout_defaults(
        self, async_client, mock_file_store
    ):
        """Plugin preset stores the configured default timeout when omitted."""
        payload = {
            "name": "Default Plugin Timeout Test",
            "plugins": [{"source": "github:owner/plugin", "ref": "main"}],
            "prompt": "Run plugin tasks",
            "trigger": {"type": "cron", "schedule": "30 10 * * 5"},
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 201
        assert response.json()["timeout"] == 600

    async def test_create_from_plugin_timeout_exceeds_max_rejected(self, async_client):
        """Plugin preset rejects timeouts over 30 minutes."""
        payload = {
            "name": "Plugin Timeout Test",
            "plugins": [{"source": "github:owner/plugin"}],
            "prompt": "Run plugin tasks",
            "trigger": {"type": "cron", "schedule": "30 10 * * 5"},
            "timeout": 1801,
        }

        response = await async_client.post(
            "/api/automation/v1/preset/plugin", json=payload
        )

        assert response.status_code == 422

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
