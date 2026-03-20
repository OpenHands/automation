"""Validation helpers for tarball_path in automations.

Supports two types of tarball sources:
1. Internal uploads: {scheme}://uploads/{uuid} (scheme configurable via env var)
2. External public URLs: https://, s3://, gs://
"""

import re
from functools import lru_cache
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import get_settings
from automation.models import TarballUpload, UploadStatus


# Valid external URL schemes (must be publicly accessible)
EXTERNAL_URL_SCHEMES = ("https://", "s3://", "gs://")


@lru_cache
def _get_internal_url_pattern() -> re.Pattern:
    """Get compiled regex pattern for internal URLs based on config."""
    scheme = get_settings().internal_url_scheme
    # Pattern: {scheme}://uploads/{uuid}
    return re.compile(
        rf"^{re.escape(scheme)}://uploads/"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        re.IGNORECASE,
    )


def get_internal_url_prefix() -> str:
    """Get the internal URL prefix (e.g., 'oh-internal://uploads/')."""
    scheme = get_settings().internal_url_scheme
    return f"{scheme}://uploads/"


def build_internal_url(upload_id: UUID) -> str:
    """Build an internal URL for an upload."""
    return f"{get_internal_url_prefix()}{upload_id}"


def parse_internal_upload_id(tarball_path: str) -> UUID | None:
    """
    Extract upload_id from internal URL.

    Returns the UUID if the path matches the internal format,
    or None if it's not an internal URL.
    """
    match = _get_internal_url_pattern().match(tarball_path)
    if match:
        return UUID(match.group(1))
    return None


def is_internal_url(tarball_path: str) -> bool:
    """Check if the tarball_path is an internal upload URL."""
    scheme = get_settings().internal_url_scheme
    return tarball_path.startswith(f"{scheme}://")


def is_valid_external_url(tarball_path: str) -> bool:
    """Check if the tarball_path has a valid external URL scheme."""
    return tarball_path.startswith(EXTERNAL_URL_SCHEMES)


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
    - Just validates the scheme (actual accessibility is checked at runtime)

    Raises:
        HTTPException: If validation fails with appropriate status code
    """
    # Check for internal upload
    upload_id = parse_internal_upload_id(tarball_path)

    scheme = get_settings().internal_url_scheme

    if upload_id:
        await _validate_internal_upload(upload_id, user_id, org_id, session)
    elif is_valid_external_url(tarball_path):
        # External URL - scheme is valid, accessibility checked at runtime
        pass
    elif is_internal_url(tarball_path):
        # Malformed internal URL (starts with scheme:// but doesn't match pattern)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid internal upload URL format. Expected: {scheme}://uploads/{{uuid}}",
        )
    else:
        # Unknown scheme
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid tarball_path. Must be {scheme}://uploads/{{uuid}} "
                "or a public URL (https://, s3://, gs://)"
            ),
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
