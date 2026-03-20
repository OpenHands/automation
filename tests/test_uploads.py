"""Tests for tarball upload functionality."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from automation.models import TarballUpload, UploadStatus
from automation.storage.google_cloud import FileSizeLimitExceeded
from automation.uploads import (
    MAX_UPLOAD_SIZE,
    UploadResponse,
    UploadStatusEnum,
    _build_storage_path,
)


class TestBuildStoragePath:
    """Test storage path generation."""

    def test_build_storage_path(self):
        """Build storage path from IDs."""
        org_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        user_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        upload_id = uuid.UUID("33333333-3333-3333-3333-333333333333")

        path = _build_storage_path(org_id, user_id, upload_id)

        expected = (
            "uploads/11111111-1111-1111-1111-111111111111/"
            "22222222-2222-2222-2222-222222222222/"
            "33333333-3333-3333-3333-333333333333.tar"
        )
        assert path == expected


class TestUploadResponse:
    """Test UploadResponse schema."""

    def test_from_model_completed(self):
        """Create response from completed upload."""
        from automation.utils.tarball_validation import build_internal_url

        upload = MagicMock(spec=TarballUpload)
        upload_id = uuid.uuid4()
        upload.id = upload_id
        upload.user_id = uuid.uuid4()
        upload.org_id = uuid.uuid4()
        upload.name = "test-upload"
        upload.description = "Test description"
        upload.status = UploadStatus.COMPLETED
        upload.error_message = None
        upload.size_bytes = 1024
        upload.storage_path = "uploads/org/user/id.tar"
        upload.created_at = MagicMock()
        upload.created_at.isoformat.return_value = "2024-01-01T00:00:00"
        upload.updated_at = MagicMock()
        upload.updated_at.isoformat.return_value = "2024-01-01T00:00:00"

        response = UploadResponse.from_model(upload)

        assert response.status == UploadStatusEnum.COMPLETED
        # tarball_path uses configurable internal URL scheme
        assert response.tarball_path == build_internal_url(upload_id)

    def test_from_model_failed(self):
        """Create response from failed upload."""
        upload = MagicMock(spec=TarballUpload)
        upload.id = uuid.uuid4()
        upload.user_id = uuid.uuid4()
        upload.org_id = uuid.uuid4()
        upload.name = "test-upload"
        upload.description = None
        upload.status = UploadStatus.FAILED
        upload.error_message = "File too large"
        upload.size_bytes = 2000000
        upload.storage_path = "uploads/org/user/id.tar"
        upload.created_at = MagicMock()
        upload.created_at.isoformat.return_value = "2024-01-01T00:00:00"
        upload.updated_at = MagicMock()
        upload.updated_at.isoformat.return_value = "2024-01-01T00:00:00"

        response = UploadResponse.from_model(upload)

        assert response.status == UploadStatusEnum.FAILED
        assert response.tarball_path is None  # Not exposed for failed uploads
        assert response.error_message == "File too large"


class TestWriteStream:
    """Test streaming write functionality."""

    @pytest.mark.asyncio
    async def test_write_stream_success(self):
        """Stream upload completes successfully."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()
            mock_file = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob
            mock_blob.open.return_value.__enter__ = MagicMock(return_value=mock_file)
            mock_blob.open.return_value.__exit__ = MagicMock(return_value=False)

            from automation.storage import GoogleCloudFileStore

            store = GoogleCloudFileStore(bucket_name="test-bucket")

            async def mock_stream():
                yield b"chunk1"
                yield b"chunk2"
                yield b"chunk3"

            size = await store.write_stream(
                path="test/file.tar",
                stream=mock_stream(),
                max_size=1000,
            )

            assert size == 18  # len("chunk1") * 3
            # Verify blob.open was called with "wb"
            mock_blob.open.assert_called_once_with("wb")
            # Verify all chunks were written
            assert mock_file.write.call_count == 3

    @pytest.mark.asyncio
    async def test_write_stream_exceeds_limit(self):
        """Stream upload fails when size limit exceeded."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()
            mock_file = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob
            mock_blob.open.return_value.__enter__ = MagicMock(return_value=mock_file)
            mock_blob.open.return_value.__exit__ = MagicMock(return_value=False)

            from automation.storage import GoogleCloudFileStore

            store = GoogleCloudFileStore(bucket_name="test-bucket")

            async def mock_stream():
                yield b"a" * 500
                yield b"b" * 500
                yield b"c" * 500  # This chunk exceeds the limit

            with pytest.raises(FileSizeLimitExceeded) as exc_info:
                await store.write_stream(
                    path="test/file.tar",
                    stream=mock_stream(),
                    max_size=1000,
                )

            assert exc_info.value.max_size == 1000
            assert exc_info.value.actual_size == 1500
            # First two chunks should have been written before failure
            assert mock_file.write.call_count == 2


class TestFileSizeLimitExceeded:
    """Test FileSizeLimitExceeded exception."""

    def test_exception_message(self):
        """Exception includes size information."""
        exc = FileSizeLimitExceeded(max_size=1000, actual_size=1500)
        assert "1500" in str(exc)
        assert "1000" in str(exc)
        assert exc.max_size == 1000
        assert exc.actual_size == 1500


class TestMaxUploadSize:
    """Test upload size constant."""

    def test_max_upload_size_is_1mb(self):
        """Maximum upload size is 1MB."""
        assert MAX_UPLOAD_SIZE == 1 * 1024 * 1024
