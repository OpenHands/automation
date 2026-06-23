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
import os
import tarfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, authenticate_request
from openhands.automation.constants import MODEL_PROFILE_PATTERN
from openhands.automation.db import get_session
from openhands.automation.models import Automation, TarballUpload, UploadStatus
from openhands.automation.schemas import AutomationResponse, Trigger
from openhands.automation.storage import FileStore, get_file_store
from openhands.automation.utils import utcnow
from openhands.automation.utils.model_profiles import resolve_model_profile_for_user
from openhands.automation.utils.tarball_validation import (
    build_internal_url,
    parse_internal_upload_id,
)
from openhands.sdk.plugin import PluginSource
from openhands.workspace import RepoSource


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/preset", tags=["Presets"])

# Preset files directories
PRESETS_DIR = Path(__file__).parent / "presets"
PROMPT_PRESET_DIR = PRESETS_DIR / "prompt"
PLUGIN_PRESET_DIR = PRESETS_DIR / "plugin"


def _get_preset_entrypoint() -> str:
    """Return the preset entrypoint for the current host platform.

    Preset automations create their virtual environment inside the run working
    directory. Cloud sandboxes use the POSIX layout (``.venv/bin/python``),
    while native Windows uses ``.venv/Scripts/python.exe``.
    """
    python_path = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    return f"{python_path} main.py"


# Preset file caches to avoid I/O on every request
_PROMPT_PRESET_CACHE: dict[str, str] | None = None
_PLUGIN_PRESET_CACHE: dict[str, str] | None = None


def _load_prompt_preset_files() -> dict[str, str]:
    """Load and cache prompt preset files from disk.

    Preset files are cached at module level to avoid I/O on every request.
    """
    global _PROMPT_PRESET_CACHE
    if _PROMPT_PRESET_CACHE is None:
        _PROMPT_PRESET_CACHE = {
            "main.py": (PROMPT_PRESET_DIR / "sdk_main.py").read_text(),
            "setup.sh": (PROMPT_PRESET_DIR / "setup.sh").read_text(),
            # Shared stdlib-only helper shipped alongside main.py so it can be
            # imported as ``from _termination import ...`` inside the sandbox.
            "_termination.py": (PRESETS_DIR / "_termination.py").read_text(),
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
            "_termination.py": (PRESETS_DIR / "_termination.py").read_text(),
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
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
        description=(
            "Model profile name to use for automation runs. Defaults to the active "
            "profile at creation time when omitted."
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
    - repos_config.json: (optional) Repository configuration for cloning

    Note: Clone and skill loading functionality is now provided by the SDK's
    OpenHandsCloudWorkspace.clone_repos() and load_skills_from_agent_server()
    methods, so separate scripts are no longer needed.

    Args:
        prompt: The user's prompt text
        repos: Optional list of repositories to clone

    Returns:
        bytes: The tarball content as bytes
    """
    preset_files = _load_prompt_preset_files()
    tarball_buffer = io.BytesIO()

    with tarfile.open(fileobj=tarball_buffer, mode="w:gz") as tar:
        _add_file_to_tar(tar, "main.py", preset_files["main.py"])
        _add_file_to_tar(tar, "_termination.py", preset_files["_termination.py"])
        _add_file_to_tar(tar, "prompt.txt", prompt)
        _add_file_to_tar(tar, "setup.sh", preset_files["setup.sh"], mode=0o755)

        # Add repos config if repos specified (SDK workspace handles cloning)
        if repos:
            repos_config = [r.model_dump(exclude_none=True) for r in repos]
            _add_file_to_tar(
                tar, "repos_config.json", json.dumps(repos_config, indent=2)
            )

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


def _replace_prompt_in_tarball(tarball_bytes: bytes, new_prompt: str) -> bytes | None:
    """Return a copy of a preset tarball with ``prompt.txt`` swapped for ``new_prompt``.

    Every other member (``main.py``, ``setup.sh``, ``plugins_config.json``,
    ``repos_config.json``, ...) is copied through unchanged, so plugin and repo
    configuration are preserved and the working template is untouched.

    Returns ``None`` if the archive has no ``prompt.txt`` member — i.e. it is not a
    regenerable preset tarball — so the caller can leave the tarball as-is.
    """
    out_buffer = io.BytesIO()
    found = False
    with (
        tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as src,
        tarfile.open(fileobj=out_buffer, mode="w:gz") as dst,
    ):
        for member in src.getmembers():
            if member.name == "prompt.txt":
                found = True
                _add_file_to_tar(
                    dst, "prompt.txt", new_prompt, mode=member.mode or 0o644
                )
                continue
            if member.isfile():
                extracted = src.extractfile(member)
                data = extracted.read() if extracted is not None else b""
                info = tarfile.TarInfo(name=member.name)
                info.size = len(data)
                info.mode = member.mode
                info.mtime = member.mtime
                dst.addfile(info, io.BytesIO(data))
            else:
                dst.addfile(member)

    if not found:
        return None

    out_buffer.seek(0)
    return out_buffer.read()


async def regenerate_preset_prompt_tarball(
    automation: Automation,
    new_prompt: str,
    session: AsyncSession,
) -> str | None:
    """Rebuild a preset automation's tarball with an updated prompt.

    Preset automations bake the prompt into ``prompt.txt`` inside the tarball the
    dispatcher executes; the stored ``prompt`` column is metadata only. When the
    prompt is edited the tarball must be rewritten too, otherwise dispatching keeps
    running the original prompt.

    Reads the automation's current internal-upload tarball, swaps in ``new_prompt``
    (leaving all other files untouched), uploads the result as a new internal upload,
    and returns its ``oh-internal://`` URL for the caller to store on ``tarball_path``.

    Returns ``None`` — leaving the tarball unchanged — when the automation is not a
    regenerable preset: its ``tarball_path`` is an external URL, the referenced upload
    is missing, or the archive contains no ``prompt.txt``. The file store is resolved
    lazily so that updates to non-preset automations never construct one.
    """
    upload_id = parse_internal_upload_id(automation.tarball_path)
    if upload_id is None:
        return None

    file_store = get_file_store()
    result = await session.execute(
        select(TarballUpload).where(TarballUpload.id == upload_id)
    )
    source_upload = result.scalars().first()
    if source_upload is None:
        return None

    try:
        current_tarball = file_store.read(source_upload.storage_path)
    except FileNotFoundError:
        return None

    new_tarball = _replace_prompt_in_tarball(current_tarball, new_prompt)
    if new_tarball is None:
        return None

    new_upload_id = uuid.uuid4()
    storage_path = _build_storage_path(
        automation.org_id, automation.user_id, new_upload_id
    )
    upload = TarballUpload(
        id=new_upload_id,
        user_id=automation.user_id,
        org_id=automation.org_id,
        name=f"prompt-automation-{_safe_truncate(automation.name, 50)}-edit",
        description=f"Prompt updated for: {_safe_truncate(automation.name, 100)}",
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    try:
        size_bytes = await file_store.write_stream(
            path=storage_path,
            stream=_bytes_to_async_iter(new_tarball),
            content_type="application/x-tar",
        )
        upload.status = UploadStatus.COMPLETED
        upload.size_bytes = size_bytes
    except Exception as e:
        # The session is rolled back when the HTTPException propagates (see
        # get_session), so don't flush here — the in-memory status/error_message
        # are only for log/debug context and won't be persisted.
        logger.exception("Failed to upload regenerated tarball: %s", e)
        upload.status = UploadStatus.FAILED
        upload.error_message = f"Upload failed: {e!s}"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload regenerated tarball: {e!s}",
        )

    # The old tarball is now superseded. Remove its file and soft-delete the
    # upload record so repeated prompt edits don't accumulate orphaned storage.
    # Only soft-delete once the file is confirmed gone: if the delete fails the
    # record stays live so the still-present file remains discoverable for a
    # later retry/cleanup instead of becoming a hidden orphan (file on disk,
    # record marked deleted).
    file_removed = False
    try:
        file_store.delete(source_upload.storage_path)
        file_removed = True
    except FileNotFoundError:
        file_removed = True
    except Exception as e:
        logger.exception(
            "Failed to delete superseded tarball at %s: %s",
            source_upload.storage_path,
            e,
        )
    if file_removed:
        source_upload.deleted_at = utcnow()

    await session.flush()
    return build_internal_url(new_upload_id)


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
    model = resolve_model_profile_for_user(body.model, user)

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
            model=model,
            trigger=body.trigger.model_dump(),
            tarball_path=tarball_path,
            setup_script_path="setup.sh",
            entrypoint=_get_preset_entrypoint(),
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

MAX_VARIANTS = 10


class ExperimentVariant(BaseModel):
    """A single variant in an A/B test experiment."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    weight: int = Field(..., gt=0, description="Relative selection weight (> 0)")
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
        description=(
            "Model profile name to use when this variant is selected. Defaults "
            "to the active profile at creation time when omitted."
        ),
    )
    plugins: list[PluginSource] = Field(
        ...,
        min_length=1,
        description="Plugin(s) for this variant.",
    )


class CreatePluginAutomationRequest(BaseModel):
    """Request to create an automation using plugins."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=500)
    plugins: list[PluginSource] | None = Field(
        default=None,
        description="Plugin(s) to load. Mutually exclusive with 'variants'.",
    )
    variants: list[ExperimentVariant] | None = Field(
        default=None,
        description=(
            "A/B test variants. Each variant specifies its own plugin set and a "
            "relative weight. Mutually exclusive with 'plugins'."
        ),
    )
    experiment_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Required when using variants. A human-readable experiment name.",
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
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=MODEL_PROFILE_PATTERN,
        description=(
            "Model profile name to use for automation runs. Defaults to the active "
            "profile at creation time when omitted."
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
            if "plugins" in data and data["plugins"] is not None:
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

    @model_validator(mode="after")
    def validate_plugins_or_variants(self) -> "CreatePluginAutomationRequest":
        """Enforce mutual exclusivity between plugins and variants."""
        if (self.plugins is None) == (self.variants is None):
            raise ValueError("Exactly one of 'plugins' or 'variants' must be provided.")

        if self.variants is not None:
            if self.experiment_id is None:
                raise ValueError("'experiment_id' is required when using 'variants'.")
            if len(self.variants) < 2:
                raise ValueError("At least two variants are required for an A/B test.")
            if len(self.variants) > MAX_VARIANTS:
                raise ValueError(f"At most {MAX_VARIANTS} variants are allowed.")
            names = [v.name for v in self.variants]
            if len(names) != len(set(names)):
                raise ValueError("Variant names must be unique.")
        else:
            if self.experiment_id is not None:
                raise ValueError(
                    "'experiment_id' can only be used with 'variants', not 'plugins'."
                )

        return self


def _resolve_experiment_variant_models(
    variants: list[ExperimentVariant] | None,
    user: AuthenticatedUser,
    default_model: str | None = None,
) -> list[ExperimentVariant] | None:
    """Return variants with model profile names resolved for persistence.

    Variant-level model profiles are stored in the generated experiment config so
    each run can load the profile selected by weighted variant assignment. Missing
    variant models use the automation-level model, which itself defaults to the
    user's active profile at creation time.
    """
    if variants is None:
        return None

    return [
        variant.model_copy(
            update={
                "model": resolve_model_profile_for_user(
                    variant.model if variant.model is not None else default_model,
                    user,
                )
            }
        )
        for variant in variants
    ]


def _generate_plugin_tarball(
    plugins: list[PluginSource] | None,
    prompt: str,
    repos: list[RepoSource] | None = None,
    *,
    experiment_id: str | None = None,
    variants: list[ExperimentVariant] | None = None,
) -> bytes:
    """Generate a tarball containing SDK code, plugin config, and prompt.

    When *variants* is provided the tarball contains ``experiment_config.json``
    instead of ``plugins_config.json``.  The two are mutually exclusive.
    """
    preset_files = _load_plugin_preset_files()

    tarball_buffer = io.BytesIO()

    with tarfile.open(fileobj=tarball_buffer, mode="w:gz") as tar:
        _add_file_to_tar(tar, "main.py", preset_files["main.py"])
        _add_file_to_tar(tar, "_termination.py", preset_files["_termination.py"])
        _add_file_to_tar(tar, "prompt.txt", prompt)
        _add_file_to_tar(tar, "setup.sh", preset_files["setup.sh"], mode=0o755)

        if variants is not None:
            experiment_config = {
                "experiment_id": experiment_id,
                "variants": [
                    {
                        "model": v.model,
                        "name": v.name,
                        "weight": v.weight,
                        "plugins": [p.model_dump(exclude_none=True) for p in v.plugins],
                    }
                    for v in variants
                ],
            }
            _add_file_to_tar(
                tar,
                "experiment_config.json",
                json.dumps(experiment_config, indent=2),
            )
        else:
            assert plugins is not None  # guaranteed by caller
            plugins_config = [p.model_dump(exclude_none=True) for p in plugins]
            _add_file_to_tar(
                tar, "plugins_config.json", json.dumps(plugins_config, indent=2)
            )

        if repos:
            repos_config = [r.model_dump(exclude_none=True) for r in repos]
            _add_file_to_tar(
                tar, "repos_config.json", json.dumps(repos_config, indent=2)
            )

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
    model = resolve_model_profile_for_user(body.model, user)
    variants = _resolve_experiment_variant_models(
        body.variants, user, default_model=model
    )

    # 1. Generate tarball with SDK code, plugin/experiment config, and prompt
    tarball_content = _generate_plugin_tarball(
        body.plugins,
        body.prompt,
        repos=body.repos,
        experiment_id=body.experiment_id,
        variants=variants,
    )

    # 2. Upload tarball to storage
    upload_id = uuid.uuid4()
    storage_path = _build_storage_path(user.org_id, user.user_id, upload_id)

    # Create upload record
    if body.variants is not None:
        variant_names = ", ".join(v.name for v in body.variants)
        description = _safe_truncate(
            f"A/B experiment {body.experiment_id}: {variant_names}", 200
        )
    else:
        assert body.plugins is not None  # guaranteed by validator
        plugin_sources_str = _format_plugin_sources_for_description(body.plugins)
        truncated = _safe_truncate(plugin_sources_str, 100)
        description = f"Auto-generated with plugins: {truncated}"

    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=f"plugin-automation-{_safe_truncate(body.name, 50)}",
        description=description,
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
            model=model,
            trigger=body.trigger.model_dump(),
            tarball_path=tarball_path,
            setup_script_path="setup.sh",
            entrypoint=_get_preset_entrypoint(),
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

    log_extra: dict[str, Any] = {
        "automation_id": str(automation.id),
        "upload_id": str(upload_id),
        "prompt_length": len(body.prompt),
    }
    if body.variants is not None:
        log_extra["experiment_id"] = body.experiment_id
        log_extra["variant_count"] = len(body.variants)
    elif body.plugins is not None:
        log_extra["plugin_count"] = len(body.plugins)

    logger.info("Created automation from plugin", extra=log_extra)

    return AutomationResponse.model_validate(automation)
