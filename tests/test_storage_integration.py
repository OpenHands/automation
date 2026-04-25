"""Integration tests for GoogleCloudFileStore using fake-gcs-server.

These tests verify actual GCS behavior using a containerized emulator.
Run with: pytest tests/test_storage_integration.py -v

Requires Docker to be running.
"""

import time

import pytest
from testcontainers.core.container import DockerContainer

from automation.storage import GoogleCloudFileStore
from automation.storage.google_cloud import BUCKET_PREFIX, FileSizeLimitExceeded


class FakeGCSContainer(DockerContainer):
    """Testcontainer for fake-gcs-server.

    There is no official testcontainers module for GCS in Python, so we use
    the generic DockerContainer with fsouza/fake-gcs-server image.
    """

    def __init__(self, port: int = 4443):
        super().__init__("fsouza/fake-gcs-server:latest")
        self.port = port
        self.with_exposed_ports(port)
        self.with_command(f"-scheme http -port {port}")

    def get_emulator_host(self) -> str:
        """Get the emulator host URL."""
        host = self.get_container_host_ip()
        mapped_port = self.get_exposed_port(self.port)
        return f"http://{host}:{mapped_port}"

    def _wait_for_ready(self, timeout: int = 30):
        """Wait for the server to be ready by checking logs."""
        start = time.time()
        while time.time() - start < timeout:
            stdout, stderr = self.get_logs()
            all_logs = (stdout or b"").decode("utf-8") + (stderr or b"").decode("utf-8")
            if "server started at" in all_logs:
                return
            time.sleep(0.5)
        raise TimeoutError("fake-gcs-server did not start in time")

    def start(self):
        """Start the container and wait for it to be ready."""
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
    # Set the emulator host environment variable
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STORAGE_EMULATOR_HOST", emulator_host)
        settings = StorageSettings(
            gcs_bucket_name="test-bucket",
            storage_emulator_host=emulator_host,
        )
        store = GoogleCloudFileStore(settings=settings)
        yield store


class TestGoogleCloudFileStoreIntegration:
    """Integration tests for GoogleCloudFileStore with real GCS emulator.

    Note: This service is upload-only, so tests verify writes by accessing
    the GCS blob directly rather than through read methods.
    """

    def test_write_string(self, file_store):
        """Write string content to storage."""
        test_content = "Hello, GCS!"
        test_path = "test/hello.txt"

        file_store.write(test_path, test_content)

        # Verify by reading blob directly (not via interface)
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{test_path}")
        result = blob.download_as_text()
        assert result == test_content

    def test_write_bytes(self, file_store):
        """Write bytes content to storage."""
        test_content = b"\x00\x01\x02binary data\xff"
        test_path = "test/binary.bin"

        file_store.write(test_path, test_content)

        # Verify by reading blob directly
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{test_path}")
        result = blob.download_as_bytes()
        assert result == test_content

    def test_list_files(self, file_store):
        """List files under a prefix."""
        # Write some test files
        file_store.write("listtest/file1.txt", "content1")
        file_store.write("listtest/file2.txt", "content2")
        file_store.write("listtest/subdir/file3.txt", "content3")
        file_store.write("other/file.txt", "other")

        # List files under listtest/
        result = file_store.list("listtest/")

        assert "listtest/file1.txt" in result
        assert "listtest/file2.txt" in result
        assert "listtest/subdir/file3.txt" in result
        assert "other/file.txt" not in result

    def test_delete_file(self, file_store):
        """Delete a file from storage."""
        from google.cloud.exceptions import NotFound

        test_path = "test/to_delete.txt"
        file_store.write(test_path, "delete me")

        # Verify it exists via blob
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{test_path}")
        assert blob.download_as_text() == "delete me"

        # Delete it
        file_store.delete(test_path)

        # Verify it's gone
        with pytest.raises(NotFound):
            blob.download_as_bytes()

    def test_automation_prefix_applied(self, file_store):
        """Verify all operations use the automation/ prefix."""
        test_path = "prefix_test/file.txt"
        file_store.write(test_path, "test content")

        # Check the actual blob name in GCS includes the prefix
        blobs = list(file_store.client.list_blobs(file_store.bucket_name))
        blob_names = [b.name for b in blobs]

        # The blob should have automation/ prefix
        expected_full_path = f"{BUCKET_PREFIX}/{test_path}"
        assert any(expected_full_path in name for name in blob_names)

    @pytest.mark.asyncio
    async def test_write_stream_success(self, file_store):
        """Stream upload completes successfully."""
        test_path = "stream_test/streamed.tar"

        async def mock_stream():
            yield b"chunk1_data"
            yield b"chunk2_data"
            yield b"chunk3_data"

        size = await file_store.write_stream(
            path=test_path,
            stream=mock_stream(),
            max_size=1000,
            content_type="application/x-tar",
        )

        # Verify size
        assert size == len(b"chunk1_data") + len(b"chunk2_data") + len(b"chunk3_data")

        # Verify content
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{test_path}")
        content = blob.download_as_bytes()
        assert content == b"chunk1_datachunk2_datachunk3_data"

    @pytest.mark.asyncio
    async def test_write_stream_exceeds_limit_deletes_partial(self, file_store):
        """Stream upload that exceeds limit deletes partial upload."""
        test_path = "stream_test/oversized.tar"

        async def large_stream():
            yield b"a" * 500
            yield b"b" * 500
            yield b"c" * 500  # This exceeds the 1000 byte limit

        with pytest.raises(FileSizeLimitExceeded) as exc_info:
            await file_store.write_stream(
                path=test_path,
                stream=large_stream(),
                max_size=1000,
            )

        assert exc_info.value.max_size == 1000
        assert exc_info.value.actual_size == 1500

        # Verify the partial upload was deleted
        from google.cloud.exceptions import NotFound

        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{test_path}")
        with pytest.raises(NotFound):
            blob.download_as_bytes()

    @pytest.mark.asyncio
    async def test_write_stream_no_limit(self, file_store):
        """Stream upload without size limit."""
        test_path = "stream_test/unlimited.tar"

        async def mock_stream():
            for i in range(10):
                yield f"chunk{i}_".encode()

        size = await file_store.write_stream(
            path=test_path,
            stream=mock_stream(),
            max_size=None,  # No limit
        )

        assert size > 0
        blob = file_store.bucket.blob(f"{BUCKET_PREFIX}/{test_path}")
        content = blob.download_as_bytes()
        assert b"chunk0_" in content
        assert b"chunk9_" in content

    def test_bucket_created_automatically_for_emulator(self, gcs_emulator):
        """Verify bucket is created automatically when using emulator."""
        from automation.config import StorageSettings

        emulator_host = gcs_emulator.get_emulator_host()
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORAGE_EMULATOR_HOST", emulator_host)
            # Use a new bucket name
            settings = StorageSettings(
                gcs_bucket_name="auto-created-bucket",
                storage_emulator_host=emulator_host,
            )
            store = GoogleCloudFileStore(settings=settings)
            # Write should work without explicit bucket creation
            store.write("test.txt", "hello")
            # Verify via blob directly
            blob = store.bucket.blob(f"{BUCKET_PREFIX}/test.txt")
            assert blob.download_as_text() == "hello"
