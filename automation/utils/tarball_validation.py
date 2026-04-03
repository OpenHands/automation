"""Validation helpers for tarball_path in automations.

Supports two types of tarball sources:
1. Internal uploads: oh-internal://uploads/{uuid}
2. External public URLs: https://, s3://, gs://

This module re-exports the pure URL parsing functions from tarball_url.py
and adds validation functions that require database access.
"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import INTERNAL_URL_SCHEME
from automation.models import TarballUpload, UploadStatus

# Re-export pure URL parsing functions from the lightweight module.
# These are duplicated there to allow workflow code to import them
# without pulling in fastapi/sqlalchemy/httpx dependencies.
from automation.utils.tarball_url import (
    EXTERNAL_URL_SCHEMES,
    HTTP_URL_SCHEMES,
    INTERNAL_URL_PREFIX,
    build_internal_url,
    get_internal_url_prefix,
    is_http_url,
    is_internal_url,
    is_valid_external_url,
    parse_internal_upload_id,
)


async def validate_tarball_path(
    tarball_path: str,
    user_id: UUID,
    org_id: UUID,
    session: AsyncSession,
) -> None:
    """
    Validate tarball_path for automation creation.

    For internal uploads (oh-internal://):
    - Verifies the upload exists and is not deleted
    - Verifies the upload belongs to the same user and org
    - Verifies the upload status is COMPLETED

    For external URLs (https://, s3://, gs://):
    - Only validates the scheme is allowed
    - URL accessibility is NOT validated here - this is intentional:
      - External URLs may require auth tokens that we don't have at creation time
      - URLs may be valid now but unavailable later (or vice versa)
      - Validation would add latency to automation creation
      - The dispatcher validates accessibility when the automation runs

    Raises:
        HTTPException: If validation fails with appropriate status code
    """
    # Check for internal upload
    upload_id = parse_internal_upload_id(tarball_path)

    if upload_id:
        await _validate_internal_upload(upload_id, user_id, org_id, session)
    elif is_valid_external_url(tarball_path):
        # External URL with valid scheme - accessibility validated at dispatch time
        pass
    elif is_internal_url(tarball_path):
        # Malformed internal URL (starts with scheme:// but doesn't match pattern)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid internal upload URL format. Expected: {INTERNAL_URL_SCHEME}://uploads/{{uuid}}",
        )
    else:
        # Unknown scheme
        internal_fmt = f"{INTERNAL_URL_SCHEME}://uploads/{{uuid}}"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid tarball_path. Must be {internal_fmt} "
            "or a public URL (https://, s3://, gs://)",
        )


async def _validate_internal_upload(
    upload_id: UUID,
    user_id: UUID,
    org_id: UUID,
    session: AsyncSession,
) -> TarballUpload:
    """
    Validate an internal upload exists and is accessible.

    Returns the upload record if valid.

    Raises:
        HTTPException: 404 if not found, 403 if wrong user, 400 if deleted/not ready
    """
    result = await session.execute(
        select(TarballUpload).where(TarballUpload.id == upload_id)
    )
    upload = result.scalars().first()

    # Check existence (don't leak if it exists but belongs to different org)
    if not upload or upload.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload not found",
        )

    # Check user ownership
    if upload.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upload belongs to another user",
        )

    # Check if deleted
    if upload.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload has been deleted",
        )

    # Check upload status
    if upload.status != UploadStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload is not ready (status: {upload.status.value})",
        )

    return upload
