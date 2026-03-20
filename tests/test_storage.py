import os
from unittest.mock import MagicMock, patch

import pytest

from automation.storage import FileStore, GoogleCloudFileStore


class TestFileStoreAbstraction:
    """Test the FileStore abstract base class."""

    def test_file_store_is_abstract(self):
        """FileStore cannot be instantiated directly."""
        with pytest.raises(TypeError):
            FileStore()  # type: ignore


class TestGoogleCloudFileStore:
    """Test the GoogleCloudFileStore implementation."""

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

    def test_write_string(self):
        """Write string content to storage."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            store.write("test/path.txt", "hello world")

            mock_blob.upload_from_string.assert_called_once_with(
                "hello world", content_type="text/plain"
            )

    def test_write_bytes(self):
        """Write bytes content to storage."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            store.write("test/path.bin", b"binary data")

            mock_blob.upload_from_string.assert_called_once_with(
                b"binary data", content_type="application/octet-stream"
            )

    def test_read(self):
        """Read content from storage."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()
            mock_blob.download_as_text.return_value = "file content"

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            result = store.read("test/path.txt")

            assert result == "file content"
            mock_blob.download_as_text.assert_called_once()

    def test_list(self):
        """List files under a prefix."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob1 = MagicMock()
            mock_blob1.name = "prefix/file1.txt"
            mock_blob2 = MagicMock()
            mock_blob2.name = "prefix/file2.txt"

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_client.list_blobs.return_value = [mock_blob1, mock_blob2]

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            result = store.list("prefix/")

            assert result == ["prefix/file1.txt", "prefix/file2.txt"]
            mock_client.list_blobs.assert_called_once_with(
                "test-bucket", prefix="prefix/"
            )

    def test_delete(self):
        """Delete a file from storage."""
        with patch("automation.storage.google_cloud.storage") as mock_storage:
            mock_client = MagicMock()
            mock_bucket = MagicMock()
            mock_blob = MagicMock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            store = GoogleCloudFileStore(bucket_name="test-bucket")
            store.delete("test/path.txt")

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

                store = GoogleCloudFileStore(bucket_name="test-bucket")
                # Access bucket property to trigger initialization
                _ = store.bucket

                mock_client.create_bucket.assert_called_once_with("test-bucket")
