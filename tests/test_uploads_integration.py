"""Integration tests for upload storage layer using fake-gcs-server.

These tests verify that the storage layer properly handles uploads as used by
the upload endpoint, including streaming writes, binary content preservation,
and proper cleanup on size limit exceeded.

Run with: pytest tests/test_uploads_integration.py -v

Requires Docker to be running.
"""

import os
import time
import uuid

import pytest
from google.cloud.exceptions import NotFound
from testcontainers.core.container import DockerContainer

from automation.storage import GoogleCloudFileStore
from automation.storage.google_cloud import BUCKET_PREFIX, FileSizeLimitExceeded
from automation.uploads import MAX_UPLOAD_SIZE


class FakeGCSContainer(DockerContainer):
    """Testcontainer for fake-gcs-server."""

    def __init__(self, port: int = 4443):
        super().__init__("fsouza/fake-gcs-server:latest")
        self.port = port
        self.with_exposed_ports(port)
        self.with_command(f"-scheme http -port {port}")

    def get_emulator_host(self) -> str:
        host = self.get_container_host_ip()
        mapped_port = self.get_exposed_port(self.port)
        return f"http://{host}:{mapped_port}"

    def _wait_for_ready(self, timeout: int = 30):
        start = time.time()
        while time.time() - start < timeout:
            stdout, stderr = self.get_logs()
            all_logs = (stdout or b"").decode("utf-8") + (stderr or b"").decode("utf-8")
            if "server started at" in all_logs:
                return
            time.sleep(0.5)
        raise TimeoutError("fake-gcs-server did not start in time")

    def start(self):
        super().start()
        self._wait_for_ready()
        return self


@pytest.fixture(scope="module")
def gcs_emulator():
    """Start fake-gcs-server container for the test module."""
    container = FakeGCSContainer()
    container.start()
    yield container
    container.stop()


@pytest.fixture
def file_store(gcs_emulator):
    """Create a GoogleCloudFileStore connected to the emulator."""
    from automation.config import StorageSettings

    emulator_host = gcs_emulator.get_emulator_host()
    original_env = os.environ.get("STORAGE_EMULATOR_HOST")
    os.environ["STORAGE_EMULATOR_HOST"] = emulator_host
    os.environ["GCS_BUCKET_NAME"] = "test-bucket"
    settings = StorageSettings(
        gcs_bucket_name="test-bucket",
        storage_emulator_host=emulator_host,
    )
    store = GoogleCloudFileStore(settings=settings)
    yield store
    if original_env is not None:
        os.environ["STORAGE_EMULATOR_HOST"] = original_env
    else:
        os.environ.pop("STORAGE_EMULATOR_HOST", None)


class TestUploadStorageIntegration:
    """Integration tests for upload storage operations.

    These tests verify the storage operations as they would be called by the
    upload endpoint, ensuring binary content is properly stored. Content is
    verified by accessing the GCS blob directly (this service is upload-only).
    """

    def _build_storage_path(self, org_id: uuid.UUID, user_id: uuid.UUID) -> str:
        """Build storage path as the upload endpoint does."""
        upload_id = uuid.uuid4()
        return f"uploads/{org_id}/{user_id}/{upload_id}.tar"

    def _read_blob(self, file_store, path: str) -> bytes:
        """Read blob content directly from GCS for verification."""
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{path}")
        return blob.download_as_bytes()

    @pytest.mark.asyncio
    async def test_stream_upload_stores_binary_content(self, file_store):
        """Streaming upload stores binary content correctly."""
        test_content = b"test tarball content"
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        async def content_stream():
            yield test_content

        size = await file_store.write_stream(
            path, content_stream(), content_type="application/x-tar"
        )

        assert size == len(test_content)
        stored = self._read_blob(file_store, path)
        assert stored == test_content

    @pytest.mark.asyncio
    async def test_stream_upload_preserves_all_byte_values(self, file_store):
        """All byte values (0x00-0xFF) are preserved through upload."""
        # Binary content with all possible byte values
        test_content = bytes(range(256)) * 10  # 2560 bytes
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        async def content_stream():
            # Simulate chunked upload as real streaming would do
            chunk_size = 100
            for i in range(0, len(test_content), chunk_size):
                yield test_content[i : i + chunk_size]

        await file_store.write_stream(
            path, content_stream(), content_type="application/gzip"
        )

        stored = self._read_blob(file_store, path)
        assert stored == test_content
        assert len(stored) == 2560

    @pytest.mark.asyncio
    async def test_stream_upload_with_size_limit_enforced(self, file_store):
        """Size limit is enforced and partial upload is deleted."""
        # Content larger than limit
        large_content = b"x" * (MAX_UPLOAD_SIZE + 1000)
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        async def content_stream():
            chunk_size = 10000
            for i in range(0, len(large_content), chunk_size):
                yield large_content[i : i + chunk_size]

        with pytest.raises(FileSizeLimitExceeded) as exc_info:
            await file_store.write_stream(
                path,
                content_stream(),
                max_size=MAX_UPLOAD_SIZE,
                content_type="application/x-tar",
            )

        assert exc_info.value.max_size == MAX_UPLOAD_SIZE
        assert exc_info.value.actual_size > MAX_UPLOAD_SIZE

        # Verify partial upload was cleaned up
        with pytest.raises(NotFound):
            self._read_blob(file_store, path)

    @pytest.mark.asyncio
    async def test_stream_upload_without_size_limit(self, file_store):
        """Uploads succeed when no size limit is specified."""
        # Content that would exceed MAX_UPLOAD_SIZE
        large_content = b"y" * (MAX_UPLOAD_SIZE + 500)
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        async def content_stream():
            yield large_content

        # No max_size specified - should succeed
        size = await file_store.write_stream(
            path, content_stream(), content_type="application/x-tar"
        )

        assert size == len(large_content)
        stored = self._read_blob(file_store, path)
        assert stored == large_content

    def test_write_stores_binary_content(self, file_store):
        """write() stores binary content correctly."""
        # Content with bytes that would fail UTF-8 decoding
        binary_content = b"\x00\x01\x02\xff\xfe\xfd binary \x80\x81"
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        file_store.write(path, binary_content)
        stored = self._read_blob(file_store, path)

        assert stored == binary_content
        assert isinstance(stored, bytes)

    def test_content_type_preserved(self, file_store):
        """Content type is set correctly on uploaded files."""
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        file_store.write(path, b"test content")
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{path}")
        blob.reload()

        # Default content type for bytes
        assert blob.content_type == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_stream_upload_sets_content_type(self, file_store):
        """Streaming upload sets the correct content type."""
        path = self._build_storage_path(uuid.uuid4(), uuid.uuid4())

        async def content_stream():
            yield b"test"

        await file_store.write_stream(
            path, content_stream(), content_type="application/gzip"
        )

        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{path}")
        blob.reload()
        assert blob.content_type == "application/gzip"
