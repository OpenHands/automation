"""Integration tests for S3FileStore using MinIO testcontainer.

These tests verify actual S3/MinIO behavior using a real MinIO instance.
They complement the unit tests in test_storage_s3.py which use mocks.

Requirements:
- Docker must be available to run the MinIO container
- testcontainers[minio] package must be installed

Run with: pytest tests/test_storage_s3_integration.py -v
"""

import os

import pytest
from testcontainers.minio import MinioContainer

from automation.storage import S3FileStore
from automation.storage.google_cloud import FileSizeLimitExceeded


@pytest.fixture(scope="module")
def minio_container():
    """Start a MinIO container for integration tests.

    This fixture is module-scoped for efficiency - the container is
    reused across all tests in the module.
    """
    with MinioContainer() as minio:
        yield minio


@pytest.fixture
def s3_store(minio_container):
    """Create an S3FileStore connected to the MinIO container.

    Sets up environment variables and creates a store instance.
    Each test gets a fresh store but shares the container.
    """
    # Get connection details from container
    host = minio_container.get_container_host_ip()
    port = minio_container.get_exposed_port(9000)
    endpoint = f"http://{host}:{port}"

    # Set environment variables for S3FileStore
    os.environ["AWS_ACCESS_KEY_ID"] = minio_container.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = minio_container.secret_key
    os.environ["AWS_S3_ENDPOINT"] = endpoint
    os.environ["AWS_S3_SECURE"] = "false"
    os.environ["AWS_S3_AUTO_CREATE_BUCKET"] = "true"

    # Create store with a test bucket
    store = S3FileStore(bucket_name="integration-test-bucket")

    yield store

    # Cleanup: remove test files (best effort)
    try:
        for path in store.list(""):
            try:
                store.delete(path)
            except FileNotFoundError:
                pass
    except Exception:
        pass  # Container may already be stopping


class TestS3FileStoreIntegration:
    """Integration tests for S3FileStore with real MinIO."""

    def test_write_and_read_string(self, s3_store):
        """Write string content and read it back."""
        test_path = "test/hello.txt"
        test_content = "Hello, MinIO!"

        s3_store.write(test_path, test_content)
        result = s3_store.read(test_path)

        assert result == test_content.encode("utf-8")

    def test_write_and_read_bytes(self, s3_store):
        """Write binary content and read it back."""
        test_path = "test/binary.bin"
        test_content = b"\x00\x01\x02\x03\xff\xfe\xfd"

        s3_store.write(test_path, test_content)
        result = s3_store.read(test_path)

        assert result == test_content

    def test_write_overwrite(self, s3_store):
        """Overwrite existing file with new content."""
        test_path = "test/overwrite.txt"

        s3_store.write(test_path, "original content")
        s3_store.write(test_path, "new content")
        result = s3_store.read(test_path)

        assert result == b"new content"

    def test_read_nonexistent_file(self, s3_store):
        """Reading non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            s3_store.read("test/nonexistent.txt")

    def test_delete_file(self, s3_store):
        """Delete a file and verify it's gone."""
        test_path = "test/to_delete.txt"

        s3_store.write(test_path, "delete me")
        s3_store.delete(test_path)

        with pytest.raises(FileNotFoundError):
            s3_store.read(test_path)

    def test_delete_nonexistent_file(self, s3_store):
        """Deleting non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            s3_store.delete("test/never_existed.txt")

    def test_list_files(self, s3_store):
        """List files under a prefix."""
        # Create multiple files
        s3_store.write("users/user1/file1.txt", "content1")
        s3_store.write("users/user1/file2.txt", "content2")
        s3_store.write("users/user2/file1.txt", "content3")
        s3_store.write("other/file.txt", "content4")

        # List files under users/user1/
        result = s3_store.list("users/user1/")

        assert len(result) == 2
        assert "users/user1/file1.txt" in result
        assert "users/user1/file2.txt" in result

    def test_list_empty_prefix(self, s3_store):
        """List with prefix that has no files returns empty list."""
        result = s3_store.list("nonexistent/prefix/")
        assert result == []

    def test_path_prefix_isolation(self, s3_store):
        """Verify files are stored under automation/ prefix."""
        test_path = "isolated/test.txt"
        s3_store.write(test_path, "isolated content")

        # The file should be accessible via the store
        result = s3_store.read(test_path)
        assert result == b"isolated content"

        # List should return the path without the automation/ prefix
        files = s3_store.list("isolated/")
        assert "isolated/test.txt" in files

    def test_large_file(self, s3_store):
        """Write and read a larger file (1MB)."""
        test_path = "test/large_file.bin"
        # 1MB of data
        test_content = b"x" * (1024 * 1024)

        s3_store.write(test_path, test_content)
        result = s3_store.read(test_path)

        assert len(result) == len(test_content)
        assert result == test_content

    def test_special_characters_in_path(self, s3_store):
        """Paths with special characters work correctly."""
        test_path = "test/special chars/file-name_v2.txt"
        test_content = "special content"

        s3_store.write(test_path, test_content)
        result = s3_store.read(test_path)

        assert result == test_content.encode("utf-8")


class TestS3FileStoreWriteStreamIntegration:
    """Integration tests for async write_stream method."""

    @pytest.mark.asyncio
    async def test_write_stream_success(self, s3_store):
        """Stream upload works end-to-end."""
        test_path = "test/streamed.tar"

        async def mock_stream():
            yield b"header_data_"
            yield b"content_data_"
            yield b"footer_data"

        size = await s3_store.write_stream(
            path=test_path,
            stream=mock_stream(),
            max_size=1024 * 1024,  # 1MB limit
            content_type="application/x-tar",
        )

        assert size == len(b"header_data_content_data_footer_data")

        # Verify content was written correctly
        result = s3_store.read(test_path)
        assert result == b"header_data_content_data_footer_data"

    @pytest.mark.asyncio
    async def test_write_stream_size_limit(self, s3_store):
        """Stream upload enforces size limit."""
        test_path = "test/oversized.tar"

        async def large_stream():
            for i in range(100):
                yield b"x" * 100  # 100 chunks of 100 bytes = 10KB

        with pytest.raises(FileSizeLimitExceeded) as exc_info:
            await s3_store.write_stream(
                path=test_path,
                stream=large_stream(),
                max_size=1000,  # 1KB limit
            )

        assert exc_info.value.max_size == 1000

        # File should not exist after failed upload
        with pytest.raises(FileNotFoundError):
            s3_store.read(test_path)

    @pytest.mark.asyncio
    async def test_write_stream_default_size_limit(self, s3_store):
        """Stream upload uses default 100MB limit when max_size=None."""
        test_path = "test/default_limit.bin"

        async def small_stream():
            yield b"small content"

        # Should succeed with small content (under default 100MB limit)
        size = await s3_store.write_stream(
            path=test_path,
            stream=small_stream(),
            max_size=None,  # Uses default 100MB limit
        )

        assert size == len(b"small content")
        result = s3_store.read(test_path)
        assert result == b"small content"


class TestS3FileStoreErrorHandling:
    """Integration tests for error handling with real errors."""

    def test_error_preserves_cause(self, s3_store):
        """FileNotFoundError preserves original exception as cause."""
        try:
            s3_store.read("test/nonexistent_for_error_test.txt")
            pytest.fail("Expected FileNotFoundError")
        except FileNotFoundError as e:
            # Verify exception chaining - cause should be a boto3 error
            assert e.__cause__ is not None
            # boto3 errors can be ClientError or specific subclasses like NoSuchKey
            cause_type = type(e.__cause__).__name__
            assert cause_type in ("ClientError", "NoSuchKey", "NoSuchBucket")


class TestBucketAutoCreation:
    """Tests for bucket auto-creation behavior."""

    def test_bucket_created_when_enabled(self, minio_container):
        """Bucket is created when AWS_S3_AUTO_CREATE_BUCKET=true."""
        host = minio_container.get_container_host_ip()
        port = minio_container.get_exposed_port(9000)
        endpoint = f"http://{host}:{port}"

        os.environ["AWS_ACCESS_KEY_ID"] = minio_container.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = minio_container.secret_key
        os.environ["AWS_S3_ENDPOINT"] = endpoint
        os.environ["AWS_S3_SECURE"] = "false"
        os.environ["AWS_S3_AUTO_CREATE_BUCKET"] = "true"

        # Create store with a unique bucket name
        unique_bucket = "auto-created-bucket-test"
        store = S3FileStore(bucket_name=unique_bucket)

        # Bucket should work (was auto-created)
        store.write("test.txt", "works")
        assert store.read("test.txt") == b"works"

    def test_bucket_not_created_when_disabled(self, minio_container):
        """Bucket is NOT created when AWS_S3_AUTO_CREATE_BUCKET=false."""
        host = minio_container.get_container_host_ip()
        port = minio_container.get_exposed_port(9000)
        endpoint = f"http://{host}:{port}"

        os.environ["AWS_ACCESS_KEY_ID"] = minio_container.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = minio_container.secret_key
        os.environ["AWS_S3_ENDPOINT"] = endpoint
        os.environ["AWS_S3_SECURE"] = "false"
        os.environ["AWS_S3_AUTO_CREATE_BUCKET"] = "false"

        # Create store with a bucket that doesn't exist
        unique_bucket = "should-not-exist-bucket"
        store = S3FileStore(bucket_name=unique_bucket)

        # Operations should fail because bucket doesn't exist
        with pytest.raises(FileNotFoundError):
            store.write("test.txt", "should fail")
