"""FastAPI router for tarball uploads."""

import uuid
from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.auth import AuthenticatedUser, authenticate_request
from automation.db import get_session
from automation.models import TarballUpload, UploadStatus
from automation.storage import FileSizeLimitExceeded, GoogleCloudFileStore
from automation.utils import utcnow


router = APIRouter(prefix="/api/v1/uploads", tags=["Uploads"])

# Maximum upload size: 1MB
MAX_UPLOAD_SIZE = 1 * 1024 * 1024


def get_file_store() -> GoogleCloudFileStore:
    """Dependency to get the file store instance."""
    return GoogleCloudFileStore()


# --- Schemas ---


class UploadStatusEnum(StrEnum):
    """Upload status for API responses."""

    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UploadResponse(BaseModel):
    """Response for a single upload."""

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    description: str | None
    status: UploadStatusEnum
    error_message: str | None
    size_bytes: int | None
    tarball_path: str | None  # Only set when status is COMPLETED
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, upload: TarballUpload) -> "UploadResponse":
        """Create response from database model."""
        # Only expose tarball_path when upload is completed
        tarball_path = None
        if upload.status == UploadStatus.COMPLETED:
            tarball_path = f"gs://{upload.storage_path}"

        return cls(
            id=upload.id,
            user_id=upload.user_id,
            org_id=upload.org_id,
            name=upload.name,
            description=upload.description,
            status=UploadStatusEnum(upload.status.value),
            error_message=upload.error_message,
            size_bytes=upload.size_bytes,
            tarball_path=tarball_path,
            created_at=upload.created_at.isoformat(),
            updated_at=upload.updated_at.isoformat(),
        )


class UploadListResponse(BaseModel):
    """Response for listing uploads."""

    uploads: list[UploadResponse]
    total: int


# --- Helper Functions ---


def _build_storage_path(
    org_id: uuid.UUID, user_id: uuid.UUID, upload_id: uuid.UUID
) -> str:
    """Build the storage path for an upload.

    Path format: uploads/{org_id}/{user_id}/{upload_id}.tar
    Note: The 'automation/' prefix is added by GoogleCloudFileStore.
    """
    return f"uploads/{org_id}/{user_id}/{upload_id}.tar"


# --- Endpoints ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_upload(
    request: Request,
    name: str = Query(..., min_length=1, max_length=255),
    description: str | None = Query(default=None, max_length=2000),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    file_store: GoogleCloudFileStore = Depends(get_file_store),
) -> UploadResponse:
    """Upload a tarball for use in automations.

    Streams the file directly to GCS with a 1MB size limit. If the upload
    exceeds the limit, streaming stops immediately and the upload is marked
    as FAILED. The partial upload remains in storage until explicitly deleted.

    The request body should be the raw tarball file content (not multipart).

    Query parameters:
    - name: A readable name for the upload (required, max 255 chars)
    - description: Optional description (max 2000 chars)

    Headers:
    - Content-Type: application/x-tar or application/gzip recommended
    - Content-Length: Optional but recommended for early rejection
    """
    # Early rejection based on Content-Length header if provided
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE} bytes",
        )

    # Generate upload ID and storage path
    upload_id = uuid.uuid4()
    storage_path = _build_storage_path(user.org_id, user.user_id, upload_id)

    # Create initial database record with UPLOADING status
    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=name,
        description=description,
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    # Stream upload directly to GCS
    try:
        size_bytes = await file_store.write_stream(
            path=storage_path,
            stream=request.stream(),
            max_size=MAX_UPLOAD_SIZE,
            content_type=request.headers.get("content-type", "application/x-tar"),
        )

        # Update record on success
        upload.status = UploadStatus.COMPLETED
        upload.size_bytes = size_bytes

    except FileSizeLimitExceeded as e:
        # Mark as failed but don't delete from storage
        upload.status = UploadStatus.FAILED
        upload.error_message = f"File size exceeds limit of {MAX_UPLOAD_SIZE} bytes"
        upload.size_bytes = e.actual_size

    await session.flush()
    await session.refresh(upload)

    return UploadResponse.from_model(upload)


@router.get("")
async def list_uploads(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: UploadStatusEnum | None = Query(default=None, alias="status"),
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> UploadListResponse:
    """List uploads for the authenticated user.

    Excludes soft-deleted uploads. Can filter by status.
    """
    base_query = select(TarballUpload).where(
        TarballUpload.user_id == user.user_id,
        TarballUpload.org_id == user.org_id,
        TarballUpload.deleted_at.is_(None),
    )

    if status_filter is not None:
        base_query = base_query.where(
            TarballUpload.status == UploadStatus(status_filter.value)
        )

    # Count total
    count_result = await session.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    # Fetch paginated results
    result = await session.execute(
        base_query.order_by(TarballUpload.created_at.desc()).offset(offset).limit(limit)
    )
    uploads = result.scalars().all()

    return UploadListResponse(
        uploads=[UploadResponse.from_model(u) for u in uploads],
        total=total,
    )


@router.get("/{upload_id}")
async def get_upload(
    upload_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    """Get a single upload by ID."""
    upload = await _get_user_upload(session, upload_id, user.user_id, user.org_id)
    return UploadResponse.from_model(upload)


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload(
    upload_id: uuid.UUID,
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    file_store: GoogleCloudFileStore = Depends(get_file_store),
) -> None:
    """Delete an upload.

    This soft-deletes the database record and removes the file from storage.
    """
    upload = await _get_user_upload(session, upload_id, user.user_id, user.org_id)

    # Delete from storage (ignore errors if file doesn't exist)
    try:
        file_store.delete(upload.storage_path)
    except Exception:
        # File may not exist (e.g., failed upload with no data)
        pass

    # Soft delete the record
    upload.deleted_at = utcnow()
    await session.flush()


# --- Helpers ---


async def _get_user_upload(
    session: AsyncSession,
    upload_id: uuid.UUID,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> TarballUpload:
    """Fetch a non-deleted upload, ensuring it belongs to the given user and org."""
    result = await session.execute(
        select(TarballUpload).where(
            TarballUpload.id == upload_id,
            TarballUpload.user_id == user_id,
            TarballUpload.org_id == org_id,
            TarballUpload.deleted_at.is_(None),
        )
    )
    upload = result.scalars().first()
    if upload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload not found",
        )
    return upload
