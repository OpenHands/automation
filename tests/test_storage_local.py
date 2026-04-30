"""Unit tests for LocalFileStore.

These tests verify the local filesystem storage backend works correctly.
Unlike GCS/S3 tests, these don't require mocks since they use real filesystem
operations via temporary directories.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from automation.config import StorageSettings, clear_config_cache
from automation.storage import LocalFileStore, get_file_store
from automation.storage.google_cloud import BUCKET_PREFIX, FileSizeLimitExceeded


def make_local_settings(base_path: str, **kwargs) -> StorageSettings:
    """Create StorageSettings for local backend."""
    return StorageSettings(
        file_store="local",
        local_storage_path=base_path,
        **kwargs,
    )


class TestLocalFileStore:
    """Unit tests for LocalFileStore using real filesystem operations."""

    def test_init_creates_base_directory(self, tmp_path: Path):
        """Initialize creates the base directory if it doesn't exist."""
        base_path = tmp_path / "storage"
        assert not base_path.exists()

        store = LocalFileStore(base_path)

        assert store.base_path == base_path
        assert base_path.exists()
        assert base_path.is_dir()

    def test_init_with_existing_directory(self, tmp_path: Path):
        """Initialize works with an existing directory."""
        store = LocalFileStore(tmp_path)
        assert store.base_path == tmp_path

    def test_init_accepts_string_path(self, tmp_path: Path):
        """Initialize accepts string path and converts to Path."""
        store = LocalFileStore(str(tmp_path))
        assert isinstance(store.base_path, Path)
        assert store.base_path == tmp_path

    def test_prefixed_path(self, tmp_path: Path):
        """Paths are prefixed with automation/."""
        store = LocalFileStore(tmp_path)
        assert store._prefixed_path("test/path.txt") == "automation/test/path.txt"
        assert store._prefixed_path("/test/path.txt") == "automation/test/path.txt"

    def test_write_string(self, tmp_path: Path):
        """Write string content to storage with automation prefix."""
        store = LocalFileStore(tmp_path)
        store.write("test/path.txt", "hello world")

        # Verify file was created with correct content
        full_path = tmp_path / "automation" / "test" / "path.txt"
        assert full_path.exists()
        assert full_path.read_text() == "hello world"

    def test_write_bytes(self, tmp_path: Path):
        """Write bytes content to storage with automation prefix."""
        store = LocalFileStore(tmp_path)
        store.write("test/path.bin", b"binary data")

        full_path = tmp_path / "automation" / "test" / "path.bin"
        assert full_path.exists()
        assert full_path.read_bytes() == b"binary data"

    def test_write_creates_parent_directories(self, tmp_path: Path):
        """Write creates parent directories as needed."""
        store = LocalFileStore(tmp_path)
        store.write("deeply/nested/path/file.txt", "content")

        full_path = tmp_path / "automation" / "deeply" / "nested" / "path" / "file.txt"
        assert full_path.exists()

    def test_read_returns_bytes(self, tmp_path: Path):
        """Read returns bytes content."""
        store = LocalFileStore(tmp_path)
        store.write("test/file.txt", "hello")

        result = store.read("test/file.txt")

        assert result == b"hello"
        assert isinstance(result, bytes)

    def test_read_not_found(self, tmp_path: Path):
        """Read raises FileNotFoundError when file doesn't exist."""
        store = LocalFileStore(tmp_path)

        with pytest.raises(FileNotFoundError, match="File not found"):
            store.read("nonexistent.txt")

    def test_list_files(self, tmp_path: Path):
        """List files under a prefix."""
        store = LocalFileStore(tmp_path)
        store.write("users/file1.txt", "content1")
        store.write("users/file2.txt", "content2")
        store.write("other/file3.txt", "content3")

        result = store.list("users/")

        assert sorted(result) == ["users/file1.txt", "users/file2.txt"]

    def test_list_nested_files(self, tmp_path: Path):
        """List includes nested files."""
        store = LocalFileStore(tmp_path)
        store.write("data/a.txt", "a")
        store.write("data/sub/b.txt", "b")
        store.write("data/sub/deep/c.txt", "c")

        result = store.list("data/")

        assert sorted(result) == ["data/a.txt", "data/sub/b.txt", "data/sub/deep/c.txt"]

    def test_list_empty_path(self, tmp_path: Path):
        """List returns empty list when path doesn't exist."""
        store = LocalFileStore(tmp_path)

        result = store.list("nonexistent/")

        assert result == []

    def test_list_single_file(self, tmp_path: Path):
        """List returns single file when path points to a file."""
        store = LocalFileStore(tmp_path)
        store.write("single.txt", "content")

        result = store.list("single.txt")

        assert result == ["single.txt"]

    def test_delete_file(self, tmp_path: Path):
        """Delete removes a file."""
        store = LocalFileStore(tmp_path)
        store.write("test/file.txt", "content")
        full_path = tmp_path / "automation" / "test" / "file.txt"
        assert full_path.exists()

        store.delete("test/file.txt")

        assert not full_path.exists()

    def test_delete_directory(self, tmp_path: Path):
        """Delete removes a directory and all contents."""
        store = LocalFileStore(tmp_path)
        store.write("dir/file1.txt", "content1")
        store.write("dir/sub/file2.txt", "content2")
        dir_path = tmp_path / "automation" / "dir"
        assert dir_path.exists()

        store.delete("dir")

        assert not dir_path.exists()

    def test_delete_nonexistent(self, tmp_path: Path):
        """Delete does nothing when path doesn't exist."""
        store = LocalFileStore(tmp_path)
        # Should not raise
        store.delete("nonexistent.txt")

    def test_bucket_prefix_matches_other_stores(self, tmp_path: Path):
        """Verify the bucket prefix matches GCS/S3 implementations."""
        assert BUCKET_PREFIX == "automation"


class TestLocalFileStorePathTraversal:
    """Security tests for path traversal prevention."""

    def test_path_traversal_blocked_write(self, tmp_path: Path):
        """Path traversal attempts should be blocked for write operations."""
        store = LocalFileStore(tmp_path)

        # These paths escape the base directory when combined with automation/ prefix
        malicious_paths = [
            "../../../etc/passwd",  # escapes via automation/../../../etc/passwd
            "../../secret",  # escapes via automation/../../secret
            "automation/../../../secret",  # escapes via double automation prefix
        ]

        for bad_path in malicious_paths:
            with pytest.raises(ValueError, match="Path traversal"):
                store.write(bad_path, "malicious")

    def test_path_traversal_blocked_read(self, tmp_path: Path):
        """Path traversal attempts should be blocked for read operations."""
        store = LocalFileStore(tmp_path)

        malicious_paths = [
            "../../../etc/passwd",
            "../../secret",
        ]

        for bad_path in malicious_paths:
            with pytest.raises(ValueError, match="Path traversal"):
                store.read(bad_path)

    def test_path_traversal_blocked_delete(self, tmp_path: Path):
        """Path traversal attempts should be blocked for delete operations."""
        store = LocalFileStore(tmp_path)

        with pytest.raises(ValueError, match="Path traversal"):
            store.delete("../../../important_file")

    def test_path_traversal_blocked_list(self, tmp_path: Path):
        """Path traversal attempts should be blocked for list operations."""
        store = LocalFileStore(tmp_path)

        with pytest.raises(ValueError, match="Path traversal"):
            store.list("../../../etc")

    @pytest.mark.asyncio
    async def test_path_traversal_blocked_write_stream(self, tmp_path: Path):
        """Path traversal attempts should be blocked for streaming writes."""
        store = LocalFileStore(tmp_path)

        async def mock_stream():
            yield b"malicious"

        with pytest.raises(ValueError, match="Path traversal"):
            await store.write_stream("../../../etc/passwd", mock_stream())

    def test_valid_paths_still_work(self, tmp_path: Path):
        """Ensure valid paths with dots in names still work."""
        store = LocalFileStore(tmp_path)

        # Paths with dots but no traversal should work
        store.write("file.txt", "content")
        store.write("dir.name/file.ext", "content")
        store.write("..hidden/file", "content")  # double dot at start of name

        assert store.read("file.txt") == b"content"
        assert store.read("dir.name/file.ext") == b"content"


class TestLocalFileStoreWriteStream:
    """Tests for the async write_stream method."""

    @pytest.mark.asyncio
    async def test_write_stream_success(self, tmp_path: Path):
        """Stream upload completes successfully."""
        store = LocalFileStore(tmp_path)

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

        full_path = tmp_path / "automation" / "test" / "streamed.tar"
        assert full_path.exists()
        assert full_path.read_bytes() == b"chunk1chunk2chunk3"

    @pytest.mark.asyncio
    async def test_write_stream_exceeds_limit(self, tmp_path: Path):
        """Stream upload raises FileSizeLimitExceeded when limit exceeded."""
        store = LocalFileStore(tmp_path)

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

        # Partial file should be cleaned up
        full_path = tmp_path / "automation" / "test" / "oversized.tar"
        assert not full_path.exists()

    @pytest.mark.asyncio
    async def test_write_stream_no_limit(self, tmp_path: Path):
        """Stream upload works without size limit."""
        store = LocalFileStore(tmp_path)

        async def mock_stream():
            for i in range(10):
                yield f"chunk{i}_".encode()

        # max_size=None means no limit
        size = await store.write_stream(
            path="test/no_limit.tar",
            stream=mock_stream(),
            max_size=None,
        )

        assert size > 0
        full_path = tmp_path / "automation" / "test" / "no_limit.tar"
        assert full_path.exists()

    @pytest.mark.asyncio
    async def test_write_stream_creates_directories(self, tmp_path: Path):
        """Stream upload creates parent directories."""
        store = LocalFileStore(tmp_path)

        async def mock_stream():
            yield b"content"

        await store.write_stream(
            path="deeply/nested/file.tar",
            stream=mock_stream(),
        )

        full_path = tmp_path / "automation" / "deeply" / "nested" / "file.tar"
        assert full_path.exists()


class TestGetFileStoreFactory:
    """Test the get_file_store factory function with local backend."""

    def test_local_returns_localfilestore(self, tmp_path: Path):
        """FILE_STORE=local returns LocalFileStore."""
        with patch.dict(
            os.environ,
            {"FILE_STORE": "local", "LOCAL_STORAGE_PATH": str(tmp_path)},
        ):
            clear_config_cache()
            store = get_file_store()
            assert isinstance(store, LocalFileStore)
            assert store.base_path == tmp_path


class TestStorageSettingsValidation:
    """Test StorageSettings validation for local backend."""

    def test_local_requires_storage_path(self):
        """LOCAL_STORAGE_PATH is required when FILE_STORE=local."""
        with pytest.raises(ValueError, match="LOCAL_STORAGE_PATH is required"):
            StorageSettings(file_store="local", local_storage_path=None)

    def test_local_with_storage_path_succeeds(self, tmp_path: Path):
        """StorageSettings validates successfully with LOCAL_STORAGE_PATH."""
        settings = make_local_settings(str(tmp_path))
        assert settings.file_store == "local"
        assert settings.local_storage_path == str(tmp_path)
