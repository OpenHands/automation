"""Unit tests for S3FileStore.

NOTE: These tests use mocks to verify the S3FileStore calls the boto3
client correctly. They do NOT test actual S3/MinIO behavior.

For integration tests, a MinIO container would be needed (similar to
test_storage_integration.py using fake-gcs-server for GCS).
"""

import os
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from automation.storage import S3FileStore
from automation.storage.google_cloud import BUCKET_PREFIX, FileSizeLimitExceeded


class TestS3FileStore:
    """Unit tests for S3FileStore using mocks."""

    def test_init_with_bucket_name(self):
        """Initialize with explicit bucket name."""
        with patch("automation.storage.s3.boto3"):
            store = S3FileStore(bucket_name="test-bucket")
            assert store.bucket_name == "test-bucket"

    def test_init_from_env_var(self):
        """Initialize with bucket name from environment variable."""
        with patch.dict(os.environ, {"AWS_S3_BUCKET": "env-bucket"}):
            with patch("automation.storage.s3.boto3"):
                store = S3FileStore()
                assert store.bucket_name == "env-bucket"

    def test_init_raises_without_bucket_name(self):
        """Raise error when no bucket name provided."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AWS_S3_BUCKET", None)
            with pytest.raises(ValueError, match="Bucket name must be provided"):
                S3FileStore()

    def test_prefixed_path(self):
        """Paths are prefixed with automation/."""
        with patch("automation.storage.s3.boto3"):
            store = S3FileStore(bucket_name="test-bucket")
            assert store._prefixed_path("test/path.txt") == "automation/test/path.txt"
            assert store._prefixed_path("/test/path.txt") == "automation/test/path.txt"

    def test_write_string(self):
        """Write string content to storage with automation prefix."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            store.write("test/path.txt", "hello world")

            mock_client.put_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="automation/test/path.txt",
                Body=b"hello world",
                ContentType="text/plain",
            )

    def test_write_bytes(self):
        """Write bytes content to storage with automation prefix."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            store.write("test/path.bin", b"binary data")

            mock_client.put_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="automation/test/path.bin",
                Body=b"binary data",
                ContentType="application/octet-stream",
            )

    def test_read_returns_bytes(self):
        """Read returns bytes content."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_body = MagicMock()
            mock_body.read.return_value = b"file content"
            mock_client.get_object.return_value = {"Body": mock_body}
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            result = store.read("test/path.txt")

            assert result == b"file content"
            mock_client.get_object.assert_called_once_with(
                Bucket="test-bucket", Key="automation/test/path.txt"
            )

    def test_read_not_found(self):
        """Read raises FileNotFoundError when key doesn't exist."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            error_response = {"Error": {"Code": "NoSuchKey"}}
            mock_client.get_object.side_effect = botocore.exceptions.ClientError(
                error_response, "GetObject"
            )
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            with pytest.raises(FileNotFoundError, match="File not found"):
                store.read("test/nonexistent.txt")

    def test_list(self):
        """List files under a prefix, with automation prefix added and stripped."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_client.list_objects_v2.return_value = {
                "Contents": [
                    {"Key": "automation/users/file1.txt"},
                    {"Key": "automation/users/file2.txt"},
                ]
            }
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            result = store.list("users/")

            assert result == ["users/file1.txt", "users/file2.txt"]
            mock_client.list_objects_v2.assert_called_once_with(
                Bucket="test-bucket", Prefix="automation/users/"
            )

    def test_list_empty(self):
        """List returns empty list when no files match."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_client.list_objects_v2.return_value = {}
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            result = store.list("empty/")

            assert result == []

    def test_delete(self):
        """Delete a file from storage with automation prefix."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            store.delete("test/path.txt")

            mock_client.head_object.assert_called_once_with(
                Bucket="test-bucket", Key="automation/test/path.txt"
            )
            mock_client.delete_object.assert_called_once_with(
                Bucket="test-bucket", Key="automation/test/path.txt"
            )

    def test_delete_not_found(self):
        """Delete raises FileNotFoundError when key doesn't exist."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            error_response = {"Error": {"Code": "404"}}
            mock_client.head_object.side_effect = botocore.exceptions.ClientError(
                error_response, "HeadObject"
            )
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")
            with pytest.raises(FileNotFoundError, match="File not found"):
                store.delete("test/nonexistent.txt")

    def test_endpoint_creates_bucket(self):
        """When using custom endpoint, bucket is created if it doesn't exist."""
        with patch.dict(os.environ, {"AWS_S3_ENDPOINT": "http://localhost:9000"}):
            with patch("automation.storage.s3.boto3") as mock_boto3:
                mock_client = MagicMock()
                error_response = {"Error": {"Code": "404"}}
                mock_client.head_bucket.side_effect = botocore.exceptions.ClientError(
                    error_response, "HeadBucket"
                )
                mock_boto3.client.return_value = mock_client

                S3FileStore(bucket_name="test-bucket")

                mock_client.create_bucket.assert_called_once_with(Bucket="test-bucket")

    def test_ensure_url_scheme_secure(self):
        """URL scheme is enforced to https when secure=True."""
        with patch("automation.storage.s3.boto3"):
            store = S3FileStore(bucket_name="test-bucket")
            assert (
                store._ensure_url_scheme(True, "example.com") == "https://example.com"
            )
            assert (
                store._ensure_url_scheme(True, "http://example.com")
                == "https://example.com"
            )
            assert (
                store._ensure_url_scheme(True, "https://example.com")
                == "https://example.com"
            )

    def test_ensure_url_scheme_insecure(self):
        """URL scheme is enforced to http when secure=False."""
        with patch("automation.storage.s3.boto3"):
            store = S3FileStore(bucket_name="test-bucket")
            assert (
                store._ensure_url_scheme(False, "example.com") == "http://example.com"
            )
            assert (
                store._ensure_url_scheme(False, "https://example.com")
                == "http://example.com"
            )
            assert (
                store._ensure_url_scheme(False, "http://example.com")
                == "http://example.com"
            )

    def test_bucket_prefix_matches_gcs(self):
        """Verify the bucket prefix matches the GCS implementation."""
        assert BUCKET_PREFIX == "automation"


class TestS3FileStoreWriteStream:
    """Tests for the async write_stream method."""

    @pytest.mark.asyncio
    async def test_write_stream_success(self):
        """Stream upload completes successfully."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")

            async def mock_stream():
                yield b"chunk1"
                yield b"chunk2"
                yield b"chunk3"

            size = await store.write_stream(
                path="test/streamed.tar",
                stream=mock_stream(),
                max_size=1000,
                content_type="application/x-tar",
            )

            assert size == 18  # len("chunk1") + len("chunk2") + len("chunk3")
            mock_client.put_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="automation/test/streamed.tar",
                Body=b"chunk1chunk2chunk3",
                ContentType="application/x-tar",
            )

    @pytest.mark.asyncio
    async def test_write_stream_exceeds_limit(self):
        """Stream upload raises FileSizeLimitExceeded when limit exceeded."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")

            async def large_stream():
                yield b"a" * 500
                yield b"b" * 500
                yield b"c" * 500  # This exceeds the 1000 byte limit

            with pytest.raises(FileSizeLimitExceeded) as exc_info:
                await store.write_stream(
                    path="test/oversized.tar",
                    stream=large_stream(),
                    max_size=1000,
                )

            assert exc_info.value.max_size == 1000
            assert exc_info.value.actual_size == 1500
            # put_object should not be called when limit is exceeded
            mock_client.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_stream_no_limit(self):
        """Stream upload without size limit."""
        with patch("automation.storage.s3.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            store = S3FileStore(bucket_name="test-bucket")

            async def mock_stream():
                for i in range(10):
                    yield f"chunk{i}_".encode()

            size = await store.write_stream(
                path="test/unlimited.tar",
                stream=mock_stream(),
                max_size=None,
            )

            assert size > 0
            mock_client.put_object.assert_called_once()
