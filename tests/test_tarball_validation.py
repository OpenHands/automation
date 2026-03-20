"""Tests for tarball_path validation."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from automation.models import TarballUpload, UploadStatus
from automation.tarball_validation import (
    EXTERNAL_URL_SCHEMES,
    build_internal_url,
    get_internal_url_prefix,
    is_internal_url,
    is_valid_external_url,
    parse_internal_upload_id,
    validate_tarball_path,
)


class TestParseInternalUploadId:
    """Tests for parse_internal_upload_id function."""

    def test_valid_internal_url(self):
        """Valid internal URL returns UUID."""
        test_uuid = uuid.UUID("12345678-1234-1234-1234-123456789abc")
        url = build_internal_url(test_uuid)
        result = parse_internal_upload_id(url)
        assert result == test_uuid

    def test_valid_internal_url_uppercase(self):
        """UUID matching is case-insensitive."""
        prefix = get_internal_url_prefix()
        url = f"{prefix}12345678-1234-1234-1234-123456789ABC"
        result = parse_internal_upload_id(url)
        assert result == uuid.UUID("12345678-1234-1234-1234-123456789abc")

    def test_invalid_uuid_returns_none(self):
        """Invalid UUID in URL returns None."""
        prefix = get_internal_url_prefix()
        url = f"{prefix}not-a-uuid"
        assert parse_internal_upload_id(url) is None

    def test_wrong_path_returns_none(self):
        """Wrong path structure returns None."""
        from automation.config import get_settings

        scheme = get_settings().internal_url_scheme
        url = f"{scheme}://files/12345678-1234-1234-1234-123456789abc"
        assert parse_internal_upload_id(url) is None

    def test_external_url_returns_none(self):
        """External URLs return None."""
        assert parse_internal_upload_id("https://example.com/file.tar") is None
        assert parse_internal_upload_id("s3://bucket/file.tar") is None
        assert parse_internal_upload_id("gs://bucket/file.tar") is None


class TestIsInternalUrl:
    """Tests for is_internal_url function."""

    def test_internal_url(self):
        """Recognizes internal URLs with configured scheme."""
        from automation.config import get_settings

        scheme = get_settings().internal_url_scheme
        assert is_internal_url(f"{scheme}://uploads/123")
        assert is_internal_url(f"{scheme}://anything")

    def test_external_url(self):
        """External URLs return False."""
        assert not is_internal_url("https://example.com")
        assert not is_internal_url("s3://bucket/path")
        assert not is_internal_url("gs://bucket/path")


class TestIsValidExternalUrl:
    """Tests for is_valid_external_url function."""

    def test_https_url(self):
        """HTTPS URLs are valid."""
        assert is_valid_external_url("https://example.com/file.tar")
        assert is_valid_external_url("https://bucket.s3.amazonaws.com/file.tar")

    def test_s3_url(self):
        """S3 URLs are valid."""
        assert is_valid_external_url("s3://bucket/path/file.tar")

    def test_gs_url(self):
        """GCS URLs are valid."""
        assert is_valid_external_url("gs://bucket/path/file.tar")

    def test_http_url_invalid(self):
        """HTTP (not HTTPS) is not valid."""
        assert not is_valid_external_url("http://example.com/file.tar")

    def test_ftp_url_invalid(self):
        """FTP URLs are not valid."""
        assert not is_valid_external_url("ftp://example.com/file.tar")

    def test_local_path_invalid(self):
        """Local paths are not valid."""
        assert not is_valid_external_url("/local/path/file.tar")
        assert not is_valid_external_url("./relative/path.tar")


class TestValidateTarballPath:
    """Tests for validate_tarball_path function."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock AsyncSession."""
        return AsyncMock()

    @pytest.fixture
    def user_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def org_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def completed_upload(self, user_id, org_id):
        """Create a completed upload model."""
        upload = MagicMock(spec=TarballUpload)
        upload.id = uuid.uuid4()
        upload.user_id = user_id
        upload.org_id = org_id
        upload.status = UploadStatus.COMPLETED
        upload.deleted_at = None
        return upload

    # --- External URL tests ---

    @pytest.mark.asyncio
    async def test_valid_https_url(self, mock_session, user_id, org_id):
        """HTTPS URLs pass validation."""
        await validate_tarball_path(
            "https://example.com/code.tar.gz",
            user_id,
            org_id,
            mock_session,
        )
        # No exception = pass

    @pytest.mark.asyncio
    async def test_valid_s3_url(self, mock_session, user_id, org_id):
        """S3 URLs pass validation."""
        await validate_tarball_path(
            "s3://bucket/path/code.tar.gz",
            user_id,
            org_id,
            mock_session,
        )

    @pytest.mark.asyncio
    async def test_valid_gs_url(self, mock_session, user_id, org_id):
        """GCS URLs pass validation."""
        await validate_tarball_path(
            "gs://bucket/path/code.tar.gz",
            user_id,
            org_id,
            mock_session,
        )

    @pytest.mark.asyncio
    async def test_invalid_scheme_raises_422(self, mock_session, user_id, org_id):
        """Invalid URL schemes raise 422."""
        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                "http://example.com/code.tar",
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 422
        assert "Invalid tarball_path" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_local_path_raises_422(self, mock_session, user_id, org_id):
        """Local paths raise 422."""
        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                "/local/path/code.tar",
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 422

    # --- Internal URL tests ---

    @pytest.mark.asyncio
    async def test_valid_internal_upload(
        self, mock_session, user_id, org_id, completed_upload
    ):
        """Valid internal upload passes validation."""
        # Setup mock to return the upload
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = completed_upload
        mock_session.execute.return_value = mock_result

        tarball_path = build_internal_url(completed_upload.id)
        await validate_tarball_path(tarball_path, user_id, org_id, mock_session)

    @pytest.mark.asyncio
    async def test_internal_upload_not_found_raises_404(
        self, mock_session, user_id, org_id
    ):
        """Non-existent upload raises 404."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = mock_result

        fake_id = uuid.uuid4()
        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                build_internal_url(fake_id),
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 404
        assert "Upload not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_internal_upload_wrong_org_raises_404(
        self, mock_session, user_id, org_id, completed_upload
    ):
        """Upload from different org raises 404 (don't leak existence)."""
        completed_upload.org_id = uuid.uuid4()  # Different org

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = completed_upload
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                build_internal_url(completed_upload.id),
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_upload_wrong_user_raises_403(
        self, mock_session, user_id, org_id, completed_upload
    ):
        """Upload from different user (same org) raises 403."""
        completed_upload.user_id = uuid.uuid4()  # Different user, same org

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = completed_upload
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                build_internal_url(completed_upload.id),
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 403
        assert "another user" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_internal_upload_deleted_raises_400(
        self, mock_session, user_id, org_id, completed_upload
    ):
        """Deleted upload raises 400."""
        completed_upload.deleted_at = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = completed_upload
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                build_internal_url(completed_upload.id),
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 400
        assert "deleted" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_internal_upload_not_completed_raises_400(
        self, mock_session, user_id, org_id, completed_upload
    ):
        """Upload that's not COMPLETED raises 400."""
        completed_upload.status = UploadStatus.UPLOADING

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = completed_upload
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                build_internal_url(completed_upload.id),
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 400
        assert "not ready" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_malformed_internal_url_raises_422(
        self, mock_session, user_id, org_id
    ):
        """Malformed internal URL raises 422."""
        from automation.config import get_settings

        scheme = get_settings().internal_url_scheme
        with pytest.raises(HTTPException) as exc_info:
            await validate_tarball_path(
                f"{scheme}://invalid/path",
                user_id,
                org_id,
                mock_session,
            )
        assert exc_info.value.status_code == 422
        assert "Invalid internal upload URL" in exc_info.value.detail


class TestExternalUrlSchemes:
    """Test that EXTERNAL_URL_SCHEMES constant is correct."""

    def test_contains_expected_schemes(self):
        """Verify expected schemes are included."""
        assert "https://" in EXTERNAL_URL_SCHEMES
        assert "s3://" in EXTERNAL_URL_SCHEMES
        assert "gs://" in EXTERNAL_URL_SCHEMES

    def test_http_not_included(self):
        """HTTP should not be in the allowed schemes."""
        assert "http://" not in EXTERNAL_URL_SCHEMES
