"""FastAPI router for prompt-based automation creation.

This endpoint allows users to create an automation by simply providing a prompt,
without needing to manually create and upload a tarball. The service generates
the SDK boilerplate code and packages it with the user's prompt.
"""

import io
import logging
import tarfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.db import get_session
from automation.models import Automation, TarballUpload, UploadStatus
from automation.schemas import AutomationResponse, CronTrigger
from automation.storage import FileStore, get_file_store
from automation.utils.tarball_validation import build_internal_url


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/template", tags=["Templates"])

# Template files directory for prompt-based automations
TEMPLATES_DIR = Path(__file__).parent / "templates" / "prompt"

# Template cache to avoid I/O on every request
_TEMPLATE_CACHE: dict[str, str] | None = None


def _load_templates() -> dict[str, str]:
    """Load and cache template files from disk.

    Templates are cached at module level to avoid I/O on every request.
    """
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        _TEMPLATE_CACHE = {
            "main.py": (TEMPLATES_DIR / "sdk_main.py").read_text(),
            "setup.sh": (TEMPLATES_DIR / "setup.sh").read_text(),
        }
    return _TEMPLATE_CACHE


def _safe_truncate(text: str, max_bytes: int) -> str:
    """Safely truncate a string to max_bytes without breaking UTF-8 characters."""
    encoded = text.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


async def _bytes_to_async_iter(data: bytes) -> AsyncIterator[bytes]:
    """Convert bytes to an async iterator yielding a single chunk."""
    yield data


class CreatePromptAutomationRequest(BaseModel):
    """Request to create an automation from a prompt."""

    name: str = Field(..., min_length=1, max_length=500)
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description="The prompt to execute in the automation",
    )
    trigger: CronTrigger
    timeout: int | None = Field(
        default=None,
        description="Maximum execution time in seconds (default: system maximum)",
    )


def _add_file_to_tar(
    tar: tarfile.TarFile, name: str, content: str, mode: int = 0o644
) -> None:
    """Add a file with the given content to the tarball."""
    content_bytes = content.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(content_bytes)
    info.mode = mode
    tar.addfile(info, io.BytesIO(content_bytes))


def _generate_tarball(prompt: str) -> bytes:
    """Generate a tarball containing SDK code and the user's prompt.

    The tarball contains:
    - main.py: SDK boilerplate that loads and executes the prompt
    - prompt.txt: The user's prompt text
    - setup.sh: Script to install the SDK

    Returns:
        bytes: The tarball content as bytes
    """
    templates = _load_templates()
    tarball_buffer = io.BytesIO()

    with tarfile.open(fileobj=tarball_buffer, mode="w:gz") as tar:
        _add_file_to_tar(tar, "main.py", templates["main.py"])
        _add_file_to_tar(tar, "prompt.txt", prompt)
        _add_file_to_tar(tar, "setup.sh", templates["setup.sh"], mode=0o755)

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
    2. Create a conversation with the user's LLM settings
    3. Execute the provided prompt
    4. Report completion status back to the automation service
    """
    # 1. Generate tarball with SDK code and prompt
    tarball_content = _generate_tarball(body.prompt)

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
