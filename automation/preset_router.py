"""FastAPI router for preset-based automation creation.

Presets are ready-to-use automation templates where users provide arguments
(like a prompt or plugin configuration) instead of writing SDK scripts.
The service generates the necessary boilerplate code and packages it into a tarball.

Currently supported presets:
- prompt: Create an automation from a natural language prompt
- plugin: Create an automation using one or more plugins
"""

import io
import json
import logging
import re
import tarfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from openhands.sdk.plugin import PluginSource
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.db import get_session
from automation.models import Automation, TarballUpload, UploadStatus
from automation.schemas import AutomationResponse, Trigger
from automation.storage import FileStore, get_file_store
from automation.utils.tarball_validation import build_internal_url


logger = logging.getLogger(__name__)

# Regex for owner/repo format (e.g., "owner/repo", "owner/repo-name", "owner/repo.name")
_OWNER_REPO_PATTERN = re.compile(r"^[\w-]+/[\w.-]+$")


# --- Repository Source Model ---


class RepoSource(BaseModel):
    """Repository source specification for cloning.

    Repositories are cloned during automation setup and skills (AGENTS.md,
    .agents/skills/, etc.) are automatically loaded from each cloned repo.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        ...,
        description=(
            "Repository identifier. Can be 'owner/repo' format (assumes GitHub) "
            "or a full URL (https://github.com/owner/repo, https://gitlab.com/owner/repo)."
        ),
    )
    ref: str | None = Field(
        default=None,
        description="Optional branch, tag, or commit SHA to checkout.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_string_input(cls, data: Any) -> Any:
        """Allow passing just a URL string instead of full object."""
        if isinstance(data, str):
            return {"url": data}
        return data

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate URL format to provide early feedback."""
        # Allow owner/repo format (e.g., "owner/repo", "my-org/my-repo.git")
        if _OWNER_REPO_PATTERN.match(v):
            return v
        # Allow full git URLs
        if v.startswith(("http://", "https://", "git@")):
            return v
        raise ValueError(
            "URL must be 'owner/repo' format or a valid git URL "
            "(https://, http://, or git@)"
        )


router = APIRouter(prefix="/v1/preset", tags=["Presets"])

# Preset files directories
PROMPT_PRESET_DIR = Path(__file__).parent / "presets" / "prompt"
PLUGIN_PRESET_DIR = Path(__file__).parent / "presets" / "plugin"
SHARED_PRESET_DIR = Path(__file__).parent / "presets" / "shared"

# Preset file caches to avoid I/O on every request
_PROMPT_PRESET_CACHE: dict[str, str] | None = None
_PLUGIN_PRESET_CACHE: dict[str, str] | None = None
_SHARED_PRESET_CACHE: dict[str, str] | None = None


def _load_shared_preset_files() -> dict[str, str]:
    """Load and cache shared preset files from disk."""
    global _SHARED_PRESET_CACHE
    if _SHARED_PRESET_CACHE is None:
        _SHARED_PRESET_CACHE = {
            "clone_repos.py": (SHARED_PRESET_DIR / "clone_repos.py").read_text(),
            "load_skills.py": (SHARED_PRESET_DIR / "load_skills.py").read_text(),
        }
    return _SHARED_PRESET_CACHE


def _load_prompt_preset_files() -> dict[str, str]:
    """Load and cache prompt preset files from disk.

    Preset files are cached at module level to avoid I/O on every request.
    """
    global _PROMPT_PRESET_CACHE
    if _PROMPT_PRESET_CACHE is None:
        _PROMPT_PRESET_CACHE = {
            "main.py": (PROMPT_PRESET_DIR / "sdk_main.py").read_text(),
            "setup.sh": (PROMPT_PRESET_DIR / "setup.sh").read_text(),
        }
    return _PROMPT_PRESET_CACHE


def _load_plugin_preset_files() -> dict[str, str]:
    """Load and cache plugin preset files from disk.

    Preset files are cached at module level to avoid I/O on every request.
    """
    global _PLUGIN_PRESET_CACHE
    if _PLUGIN_PRESET_CACHE is None:
        _PLUGIN_PRESET_CACHE = {
            "main.py": (PLUGIN_PRESET_DIR / "sdk_main.py").read_text(),
            "setup.sh": (PLUGIN_PRESET_DIR / "setup.sh").read_text(),
        }
    return _PLUGIN_PRESET_CACHE


def _safe_truncate(text: str, max_bytes: int) -> str:
    """Safely truncate a string to max_bytes without breaking UTF-8 characters."""
    encoded = text.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


async def _bytes_to_async_iter(data: bytes) -> AsyncIterator[bytes]:
    """Convert bytes to an async iterator yielding a single chunk."""
    yield data


class CreatePromptAutomationRequest(BaseModel):
    """Request to create an automation from a prompt."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=500)
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description="The prompt to execute in the automation",
    )
    trigger: Trigger = Field(
        ...,
        description=(
            "Trigger configuration. Either a cron trigger (type: 'cron') "
            "or an event trigger (type: 'event') for webhook-based automation."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description="Maximum execution time in seconds (default: system maximum)",
    )
    repos: list[RepoSource] | None = Field(
        default=None,
        description=(
            "Repository/repositories to clone. Skills (AGENTS.md, .agents/skills/) "
            "are automatically loaded from each cloned repository. "
            "Can be a single repo or a list of repos."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_repos(cls, data: Any) -> Any:
        """Normalize repos to always be a list if provided."""
        if isinstance(data, dict) and "repos" in data and data["repos"] is not None:
            repos = data["repos"]
            if isinstance(repos, (str, dict)):
                data["repos"] = [repos]
        return data


def _add_file_to_tar(
    tar: tarfile.TarFile, name: str, content: str, mode: int = 0o644
) -> None:
    """Add a file with the given content to the tarball."""
    content_bytes = content.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(content_bytes)
    info.mode = mode
    tar.addfile(info, io.BytesIO(content_bytes))


def _generate_tarball(prompt: str, repos: list[RepoSource] | None = None) -> bytes:
    """Generate a tarball containing SDK code and the user's prompt.

    The tarball contains:
    - main.py: SDK boilerplate that loads and executes the prompt
    - prompt.txt: The user's prompt text
    - setup.sh: Script to install the SDK
    - load_skills.py: Shared utility for loading skills from repos
    - clone_repos.py: (optional) Script to clone repositories
    - repos_config.json: (optional) Repository configuration for cloning

    Args:
        prompt: The user's prompt text
        repos: Optional list of repositories to clone

    Returns:
        bytes: The tarball content as bytes
    """
    preset_files = _load_prompt_preset_files()
    shared_files = _load_shared_preset_files()
    tarball_buffer = io.BytesIO()

    with tarfile.open(fileobj=tarball_buffer, mode="w:gz") as tar:
        _add_file_to_tar(tar, "main.py", preset_files["main.py"])
        _add_file_to_tar(tar, "prompt.txt", prompt)
        _add_file_to_tar(tar, "setup.sh", preset_files["setup.sh"], mode=0o755)
        # Always include load_skills.py (main.py imports it)
        _add_file_to_tar(tar, "load_skills.py", shared_files["load_skills.py"])

        # Add repos config and clone script if repos specified
        if repos:
            repos_config = [r.model_dump(exclude_none=True) for r in repos]
            _add_file_to_tar(
                tar, "repos_config.json", json.dumps(repos_config, indent=2)
            )
            _add_file_to_tar(tar, "clone_repos.py", shared_files["clone_repos.py"])

    tarball_buffer.seek(0)
    return tarball_buffer.read()


def _build_storage_path(
    org_id: uuid.UUID, user_id: uuid.UUID, upload_id: uuid.UUID
) -> str:
    """Build the storage path for an upload.

    Path format: uploads/{org_id}/{user_id}/{upload_id}.tar
    Note: The 'automation/' prefix is added by the FileStore implementation.
    """
    return f"uploads/{org_id}/{user_id}/{upload_id}.tar"


@router.post("/prompt", status_code=status.HTTP_201_CREATED)
async def create_automation_from_prompt(
    body: CreatePromptAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    file_store: FileStore = Depends(get_file_store),
) -> AutomationResponse:
    """Create an automation from a prompt.

    This endpoint simplifies automation creation by accepting just a prompt.
    The service generates SDK boilerplate code, packages it with the prompt
    into a tarball, uploads it to storage, and creates the automation.

    The generated automation will:
    1. Set up the OpenHands SDK environment
    2. Clone any specified repositories (optional)
    3. Load skills from cloned repositories (AGENTS.md, .agents/skills/)
    4. Create a conversation with the user's LLM settings
    5. Execute the provided prompt
    6. Report completion status back to the automation service
    """
    # 1. Generate tarball with SDK code, prompt, and optional repos config
    tarball_content = _generate_tarball(body.prompt, repos=body.repos)

    # 2. Upload tarball to storage
    upload_id = uuid.uuid4()
    storage_path = _build_storage_path(user.org_id, user.user_id, upload_id)

    # Create upload record with safe UTF-8 truncation
    truncated_prompt = _safe_truncate(body.prompt, 100)
    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=f"prompt-automation-{_safe_truncate(body.name, 50)}",
        description=f"Auto-generated from prompt: {truncated_prompt}...",
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    # Upload to storage using async write_stream
    try:
        size_bytes = await file_store.write_stream(
            path=storage_path,
            stream=_bytes_to_async_iter(tarball_content),
            content_type="application/x-tar",
        )
        upload.status = UploadStatus.COMPLETED
        upload.size_bytes = size_bytes
    except Exception as e:
        logger.exception("Failed to upload generated tarball: %s", e)
        upload.status = UploadStatus.FAILED
        upload.error_message = f"Upload failed: {e!s}"
        await session.flush()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload tarball: {e!s}",
        )

    await session.flush()

    # 3. Create the automation referencing the internal upload
    tarball_path = build_internal_url(upload_id)

    try:
        automation = Automation(
            user_id=user.user_id,
            org_id=user.org_id,
            name=body.name,
            prompt=body.prompt,
            trigger=body.trigger.model_dump(),
            tarball_path=tarball_path,
            setup_script_path="setup.sh",
            entrypoint="python main.py",
            timeout=body.timeout,
        )
        session.add(automation)
        await session.flush()
        await session.refresh(automation)
    except Exception as e:
        # Clean up orphaned upload on automation creation failure
        try:
            file_store.delete(storage_path)
        except Exception:
            logger.exception("Failed to clean up orphaned upload at %s", storage_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create automation: {e!s}",
        )

    logger.info(
        "Created automation from prompt",
        extra={
            "automation_id": str(automation.id),
            "upload_id": str(upload_id),
            "prompt_length": len(body.prompt),
        },
    )

    return AutomationResponse.model_validate(automation)


# --- Plugin Preset ---


class CreatePluginAutomationRequest(BaseModel):
    """Request to create an automation using plugins."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=500)
    plugins: list[PluginSource] = Field(
        ...,
        description="Plugin(s) to load. Can be a single plugin or a list of plugins. "
        "Each plugin specifies a source (github:owner/repo, git URL, or local path), "
        "optional ref (branch/tag/commit), and optional repo_path for monorepos.",
    )
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description=(
            "The prompt to execute. Can include plugin command invocations "
            "like /plugin-name:command or be a custom prompt."
        ),
    )
    trigger: Trigger = Field(
        ...,
        description=(
            "Trigger configuration. Either a cron trigger (type: 'cron') "
            "or an event trigger (type: 'event') for webhook-based automation."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description="Maximum execution time in seconds (default: system maximum)",
    )
    repos: list[RepoSource] | None = Field(
        default=None,
        description=(
            "Repository/repositories to clone. Skills (AGENTS.md, .agents/skills/) "
            "are automatically loaded from each cloned repository. "
            "Can be a single repo or a list of repos."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_plugins_and_repos(cls, data: dict) -> dict:  # type: ignore[type-arg]
        """Normalize plugins and repos to always be lists."""
        if isinstance(data, dict):
            # Normalize plugins
            if "plugins" in data:
                plugins = data["plugins"]
                if isinstance(plugins, dict):
                    data["plugins"] = [plugins]
                elif isinstance(plugins, list) and len(plugins) == 0:
                    raise ValueError("At least one plugin is required")
            # Normalize repos
            if "repos" in data and data["repos"] is not None:
                repos = data["repos"]
                if isinstance(repos, (str, dict)):
                    data["repos"] = [repos]
        return data


def _generate_plugin_tarball(
    plugins: list[PluginSource], prompt: str, repos: list[RepoSource] | None = None
) -> bytes:
    """Generate a tarball containing SDK code, plugin config, and prompt.

    The tarball contains:
    - main.py: SDK boilerplate that loads plugins and runs conversation
    - plugins_config.json: List of plugin sources (serialized PluginSource models)
    - prompt.txt: The prompt to send
    - setup.sh: Script to install the SDK
    - load_skills.py: Shared utility for loading skills from repos
    - clone_repos.py: (optional) Script to clone repositories
    - repos_config.json: (optional) Repository configuration for cloning

    Args:
        plugins: List of plugins to load
        prompt: The user's prompt text
        repos: Optional list of repositories to clone

    Returns:
        bytes: The tarball content as bytes
    """
    preset_files = _load_plugin_preset_files()
    shared_files = _load_shared_preset_files()

    # Serialize plugins using Pydantic (exclude None values for cleaner JSON)
    plugins_config = [p.model_dump(exclude_none=True) for p in plugins]
    plugins_config_json = json.dumps(plugins_config, indent=2)

    tarball_buffer = io.BytesIO()

    with tarfile.open(fileobj=tarball_buffer, mode="w:gz") as tar:
        _add_file_to_tar(tar, "main.py", preset_files["main.py"])
        _add_file_to_tar(tar, "plugins_config.json", plugins_config_json)
        _add_file_to_tar(tar, "prompt.txt", prompt)
        _add_file_to_tar(tar, "setup.sh", preset_files["setup.sh"], mode=0o755)
        # Always include load_skills.py (main.py imports it)
        _add_file_to_tar(tar, "load_skills.py", shared_files["load_skills.py"])

        # Add repos config and clone script if repos specified
        if repos:
            repos_config = [r.model_dump(exclude_none=True) for r in repos]
            _add_file_to_tar(
                tar, "repos_config.json", json.dumps(repos_config, indent=2)
            )
            _add_file_to_tar(tar, "clone_repos.py", shared_files["clone_repos.py"])

    tarball_buffer.seek(0)
    return tarball_buffer.read()


def _format_plugin_sources_for_description(plugins: list[PluginSource]) -> str:
    """Format plugin sources for use in upload description."""
    return ", ".join(f"{p.source}@{p.ref}" if p.ref else p.source for p in plugins)


@router.post("/plugin", status_code=status.HTTP_201_CREATED)
async def create_automation_from_plugin(
    body: CreatePluginAutomationRequest,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    file_store: FileStore = Depends(get_file_store),
) -> AutomationResponse:
    """Create an automation using plugins.

    This endpoint creates an automation that loads one or more plugins and
    executes a prompt. Plugins provide skills, MCP configurations, hooks,
    and commands that extend the agent's capabilities.

    The generated automation will:
    1. Set up the OpenHands SDK environment
    2. Clone any specified repositories (optional)
    3. Load skills from cloned repositories (AGENTS.md, .agents/skills/)
    4. Create a conversation with the user's LLM settings
    5. Load all specified plugins (fetched at runtime from their sources)
    6. Execute the provided prompt (which can invoke plugin commands)
    7. Report completion status back to the automation service

    Plugin sources can be:
    - GitHub shorthand: github:owner/repo
    - Git URL: https://github.com/owner/repo.git
    - With ref: branch, tag, or commit SHA
    - With repo_path: subdirectory for monorepos
    """
    # 1. Generate tarball with SDK code, plugin config, prompt, and repos config
    tarball_content = _generate_plugin_tarball(
        body.plugins, body.prompt, repos=body.repos
    )

    # 2. Upload tarball to storage
    upload_id = uuid.uuid4()
    storage_path = _build_storage_path(user.org_id, user.user_id, upload_id)

    # Create upload record
    plugin_sources_str = _format_plugin_sources_for_description(body.plugins)
    truncated_sources = _safe_truncate(plugin_sources_str, 100)
    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=f"plugin-automation-{_safe_truncate(body.name, 50)}",
        description=f"Auto-generated with plugins: {truncated_sources}",
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    # Upload to storage using async write_stream
    try:
        size_bytes = await file_store.write_stream(
            path=storage_path,
            stream=_bytes_to_async_iter(tarball_content),
            content_type="application/x-tar",
        )
        upload.status = UploadStatus.COMPLETED
        upload.size_bytes = size_bytes
    except Exception as e:
        logger.exception("Failed to upload generated tarball: %s", e)
        upload.status = UploadStatus.FAILED
        upload.error_message = f"Upload failed: {e!s}"
        await session.flush()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload tarball: {e!s}",
        )

    await session.flush()

    # 3. Create the automation referencing the internal upload
    tarball_path = build_internal_url(upload_id)

    try:
        automation = Automation(
            user_id=user.user_id,
            org_id=user.org_id,
            name=body.name,
            prompt=body.prompt,
            trigger=body.trigger.model_dump(),
            tarball_path=tarball_path,
            setup_script_path="setup.sh",
            entrypoint="python main.py",
            timeout=body.timeout,
        )
        session.add(automation)
        await session.flush()
        await session.refresh(automation)
    except Exception as e:
        # Clean up orphaned upload on automation creation failure
        try:
            file_store.delete(storage_path)
        except Exception:
            logger.exception("Failed to clean up orphaned upload at %s", storage_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create automation: {e!s}",
        )

    logger.info(
        "Created automation from plugin",
        extra={
            "automation_id": str(automation.id),
            "upload_id": str(upload_id),
            "plugin_count": len(body.plugins),
            "prompt_length": len(body.prompt),
        },
    )

    return AutomationResponse.model_validate(automation)
