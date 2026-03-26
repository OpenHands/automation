"""Unit tests for storage abstraction.

NOTE: These tests use mocks to verify the GoogleCloudFileStore calls the GCS
client correctly. They do NOT test actual GCS behavior.

For integration tests that verify real GCS behavior using fake-gcs-server,
see test_storage_integration.py (requires Docker).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from automation.storage import (
    FileStore,
    GoogleCloudFileStore,
    S3FileStore,
    get_file_store,
)
from automation.storage.google_cloud import BUCKET_PREFIX


class TestFileStoreAbstraction:
    """Test the FileStore abstract base class."""

    def test_file_store_is_abstract(self):
        """FileStore cannot be instantiated directly."""
        with pytest.raises(TypeError):
            FileStore()  # type: ignore


class TestGetFileStoreFactory:
    """Test the get_file_store factory function."""

    def test_default_returns_gcs(self):
        """Default FILE_STORE returns GoogleCloudFileStore."""
        with patch.dict(os.environ, {"GCS_BUCKET_NAME": "test-bucket"}, clear=False):
            os.environ.pop("FILE_STORE", None)
            with patch("automation.storage.google_cloud.storage"):
                store = get_file_store()
                assert isinstance(store, GoogleCloudFileStore)

    def test_gcs_explicit(self):
        """FILE_STORE=gcs returns GoogleCloudFileStore."""
        with patch.dict(
            os.environ, {"FILE_STORE": "gcs", "GCS_BUCKET_NAME": "test-bucket"}
        ):
            with patch("automation.storage.google_cloud.storage"):
                store = get_file_store()
                assert isinstance(store, GoogleCloudFileStore)

    def test_s3_returns_s3filestore(self):
        """FILE_STORE=s3 returns S3FileStore."""
        with patch.dict(
            os.environ, {"FILE_STORE": "s3", "AWS_S3_BUCKET": "test-bucket"}
        ):
            with patch("automation.storage.s3.boto3"):
                store = get_file_store()
                assert isinstance(store, S3FileStore)

    def test_case_insensitive(self):
        """FILE_STORE is case insensitive."""
        with patch.dict(
            os.environ, {"FILE_STORE": "S3", "AWS_S3_BUCKET": "test-bucket"}
        ):
            with patch("automation.storage.s3.boto3"):
                store = get_file_store()
                assert isinstance(store, S3FileStore)

    def test_unsupported_raises_error(self):
        """Unsupported FILE_STORE raises ValueError."""
        with patch.dict(os.environ, {"FILE_STORE": "unsupported"}):
            with pytest.raises(ValueError, match="Unsupported FILE_STORE type"):
                get_file_store()


class TestGoogleCloudFileStore:
    """Unit tests for GoogleCloudFileStore using mocks.

    These tests verify the class calls the GCS client correctly but do not
    test actual GCS behavior. See module docstring for integration testing.
    """

    def test_init_with_bucket_name(self):
        """Initialize with explicit bucket name."""
        with patch("automation.storage.google_cloud.storage"):
            store = GoogleCloudFileStore(bucket_name="test-bucket")
            assert store.bucket_name == "test-bucket"

    def test_init_from_env_var(self):
        """Initialize with bucket name from environment variable."""
        with patch.dict(os.environ, {"GCS_BUCKET_NAME": "env-bucket"}):
            with patch("automation.storage.google_cloud.storage"):
                store = GoogleCloudFileStore()
                assert store.bucket_name == "env-bucket"

    def test_init_raises_without_bucket_name(self):
        """Raise error when no bucket name provided."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove GCS_BUCKET_NAME if it exists
            os.environ.pop("GCS_BUCKET_NAME", None)
            with pytest.raises(ValueError, match="Bucket name must be provided"):
                GoogleCloudFileStore()

    def test_prefixed_path(self):
        """Paths are prefixed with automation/."""
        with patch("automation.storage.google_cloud.storage"):
            store = GoogleCloudFileStore(bucket_name="test-bucket")
            assert store._prefixed_path("test/path.txt") == "automation/test/path.txt"
            assert store._prefixed_path("/test/path.txt") == "automation/test/path.txt"

    def test_write_string(self):
        """Write string content to storage with automation prefix."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            store.write("test/path.txt", "hello world")

            # Verify the path is prefixed
            mock_bucket.blob.assert_called_once_with("automation/test/path.txt")
            mock_blob.upload_from_string.assert_called_once_with(
                "hello world", content_type="text/plain"
            )

    def test_write_bytes(self):
        """Write bytes content to storage with automation prefix."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            store.write("test/path.bin", b"binary data")

            # Verify the path is prefixed
            mock_bucket.blob.assert_called_once_with("automation/test/path.bin")
            mock_blob.upload_from_string.assert_called_once_with(
                b"binary data", content_type="application/octet-stream"
            )

    def test_list(self):
        """List files under a prefix, with automation prefix added and stripped."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            # Blobs have the full path including automation prefix
            mock_blob1 = MagicMock()
            mock_blob1.name = "automation/users/file1.txt"
            mock_blob2 = MagicMock()
            mock_blob2.name = "automation/users/file2.txt"

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_client.list_blobs.return_value = [mock_blob1, mock_blob2]

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            result = store.list("users/")

            # Results should have automation prefix stripped
            assert result == ["users/file1.txt", "users/file2.txt"]
            # list_blobs should be called with prefixed path
            mock_client.list_blobs.assert_called_once_with(
                "test-bucket", prefix="automation/users/"
            )

    def test_delete(self):
        """Delete a file from storage with automation prefix."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            store.delete("test/path.txt")

            # Verify the path is prefixed
            mock_bucket.blob.assert_called_once_with("automation/test/path.txt")
            mock_blob.delete.assert_called_once()

    def test_emulator_creates_bucket(self):
        """When using emulator, bucket is created if it doesn't exist."""
        with patch.dict(os.environ, {"STORAGE_EMULATOR_HOST": "http://localhost:4443"}):
            with patch("automation.storage.google_cloud.storage") as mock_storage:
                mock_client = MagicMock()
                mock_bucket = MagicMock()

                mock_storage.Client.return_value = mock_client
                mock_client.bucket.return_value = mock_bucket
                mock_client.get_bucket.side_effect = Exception("Not found")

                # Bucket creation happens during __init__ when emulator is set
                GoogleCloudFileStore(bucket_name="test-bucket")

                mock_client.create_bucket.assert_called_once_with("test-bucket")

    def test_bucket_prefix_constant(self):
        """Verify the bucket prefix constant is set correctly."""
        assert BUCKET_PREFIX == "automation"
