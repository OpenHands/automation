"""FastAPI router for prompt-based automation creation.

This endpoint allows users to create an automation by simply providing a prompt,
without needing to manually create and upload a tarball. The service generates
the SDK boilerplate code and packages it with the user's prompt.
"""

import io
import logging
import tarfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.db import get_session
from automation.models import Automation, TarballUpload, UploadStatus
from automation.schemas import AutomationResponse, CronTrigger
from automation.storage import FileStore, get_file_store
from automation.utils.tarball_validation import build_internal_url


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Automations"])

# Template files directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


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


def _generate_tarball(prompt: str) -> bytes:
    """Generate a tarball containing SDK code and the user's prompt.

    The tarball contains:
    - main.py: SDK boilerplate that loads and executes the prompt
    - prompt.txt: The user's prompt text
    - setup.sh: Script to install the SDK

    Returns:
        bytes: The tarball content as bytes
    """
    tarball_buffer = io.BytesIO()

    with tarfile.open(fileobj=tarball_buffer, mode="w:gz") as tar:
        # Add main.py from template
        main_py_path = TEMPLATES_DIR / "sdk_main.py"
        main_py_content = main_py_path.read_text()
        main_py_bytes = main_py_content.encode("utf-8")
        main_py_info = tarfile.TarInfo(name="main.py")
        main_py_info.size = len(main_py_bytes)
        tar.addfile(main_py_info, io.BytesIO(main_py_bytes))

        # Add prompt.txt with the user's prompt
        prompt_bytes = prompt.encode("utf-8")
        prompt_info = tarfile.TarInfo(name="prompt.txt")
        prompt_info.size = len(prompt_bytes)
        tar.addfile(prompt_info, io.BytesIO(prompt_bytes))

        # Add setup.sh from template
        setup_sh_path = TEMPLATES_DIR / "setup.sh"
        setup_sh_content = setup_sh_path.read_text()
        setup_sh_bytes = setup_sh_content.encode("utf-8")
        setup_sh_info = tarfile.TarInfo(name="setup.sh")
        setup_sh_info.size = len(setup_sh_bytes)
        setup_sh_info.mode = 0o755  # Make executable
        tar.addfile(setup_sh_info, io.BytesIO(setup_sh_bytes))

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


@router.post("/from-prompt", status_code=status.HTTP_201_CREATED)
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

    # Create upload record
    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=f"prompt-automation-{body.name[:50]}",
        description=f"Auto-generated from prompt: {body.prompt[:100]}...",
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    # Upload to storage (synchronous operation)
    try:
        file_store.write(path=storage_path, contents=tarball_content)
        upload.status = UploadStatus.COMPLETED
        upload.size_bytes = len(tarball_content)
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

    logger.info(
        "Created automation from prompt",
        extra={
            "automation_id": str(automation.id),
            "upload_id": str(upload_id),
            "prompt_length": len(body.prompt),
        },
    )

    return AutomationResponse.model_validate(automation)
