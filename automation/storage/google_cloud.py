from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from google.cloud import storage
from google.cloud.exceptions import NotFound

from automation.storage.file_store import FileStore


if TYPE_CHECKING:
    from automation.config import StorageSettings


class FileSizeLimitExceeded(Exception):
    """Raised when upload exceeds the maximum allowed size."""

    def __init__(self, max_size: int, actual_size: int):
        self.max_size = max_size
        self.actual_size = actual_size
        super().__init__(
            f"File size {actual_size} bytes exceeds limit of {max_size} bytes"
        )


# All files are stored under this prefix in the bucket
BUCKET_PREFIX = "automation"


class GoogleCloudFileStore(FileStore):
    """
    Google Cloud Storage file store implementation.

    Supports both real GCS and fake-gcs-server emulator.
    When STORAGE_EMULATOR_HOST is set in StorageSettings, the client
    automatically connects to the emulator instead of real GCS.

    All files are stored under the "automation/" prefix in the bucket
    to isolate automation service data from other services.
    """

    def __init__(self, settings: StorageSettings):
        """
        Initialize the Google Cloud file store.

        Args:
            settings: StorageSettings instance with GCS configuration.
        """
        self.bucket_name = settings.gcs_bucket_name
        # This should already be validated by StorageSettings, but defensive check
        if not self.bucket_name:
            raise ValueError("GCS_BUCKET_NAME is required for GCS backend")

        # Initialize client and bucket eagerly
        # When STORAGE_EMULATOR_HOST is set, the client automatically
        # connects to the emulator (e.g., fake-gcs-server)
        self.client = storage.Client()
        self.bucket = self.client.bucket(self.bucket_name)

        # For emulator: ensure bucket exists
        if settings.storage_emulator_host:
            self._ensure_bucket_exists()

    def _prefixed_path(self, path: str) -> str:
        """Add the automation prefix to a path."""
        # Remove leading slash if present
        path = path.lstrip("/")
        return f"{BUCKET_PREFIX}/{path}"

    def _ensure_bucket_exists(self) -> None:
        """Create the bucket if it doesn't exist (for emulator only)."""
        try:
            self.client.get_bucket(self.bucket_name)
        except Exception:
            # Bucket doesn't exist, create it
            self.client.create_bucket(self.bucket_name)

    def write(self, path: str, contents: str | bytes) -> None:
        """
        Write contents to a file at the given path.

        Args:
            path: The path/key in the bucket to write to (will be prefixed
                  with "automation/").
            contents: The content to write (string or bytes).
        """
        full_path = self._prefixed_path(path)
        blob = self.bucket.blob(full_path)
        if isinstance(contents, str):
            blob.upload_from_string(contents, content_type="text/plain")
        else:
            blob.upload_from_string(contents, content_type="application/octet-stream")

    def read(self, path: str) -> bytes:
        """Read file contents from GCS.

        Args:
            path: The path/key in the bucket (will be prefixed with "automation/").

        Returns:
            The file contents as bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        full_path = self._prefixed_path(path)
        blob = self.bucket.blob(full_path)
        try:
            return blob.download_as_bytes()
        except NotFound:
            raise FileNotFoundError(f"File not found: {full_path}")

    def list(self, path: str) -> list[str]:
        """
        List all files under the given path prefix.

        Args:
            path: The prefix to search for (will be prefixed with "automation/").

        Returns:
            A list of file paths matching the prefix (without the "automation/"
            prefix).
        """
        full_path = self._prefixed_path(path)
        blobs = self.client.list_blobs(self.bucket_name, prefix=full_path)
        # Strip the automation prefix from returned paths
        prefix_len = len(BUCKET_PREFIX) + 1  # +1 for the trailing slash
        return [blob.name[prefix_len:] for blob in blobs]

    def delete(self, path: str) -> None:
        """
        Delete the file at the given path.

        Args:
            path: The path/key in the bucket to delete (will be prefixed
                  with "automation/").

        Raises:
            FileNotFoundError: If the file doesn't exist.
        """
        full_path = self._prefixed_path(path)
        blob = self.bucket.blob(full_path)
        try:
            blob.delete()
        except NotFound:
            raise FileNotFoundError(f"File not found: {full_path}")

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        """
        Stream content to a file, enforcing an optional size limit.

        Streams data chunk by chunk directly to GCS. If max_size is specified
        and the total size exceeds it, the partial upload is deleted and
        FileSizeLimitExceeded is raised.

        Args:
            path: The path/key in the bucket to write to (will be prefixed
                  with "automation/").
            stream: An async iterator yielding bytes chunks.
            max_size: Maximum allowed file size in bytes. If None, no limit.
            content_type: MIME type for the uploaded file.

        Returns:
            The total number of bytes written.

        Raises:
            FileSizeLimitExceeded: If the stream exceeds max_size bytes.
        """
        full_path = self._prefixed_path(path)
        blob = self.bucket.blob(full_path)
        blob.content_type = content_type

        total_size = 0
        size_exceeded = False
        exceeded_size = 0

        # Stream directly to GCS using blob.open() for true streaming
        # Type ignore: blob.open("wb") returns BlobWriter which accepts bytes,
        # but pyright incorrectly infers it as a text writer
        with blob.open("wb") as f:  # type: ignore[arg-type]
            async for chunk in stream:
                total_size += len(chunk)
                if max_size is not None and total_size > max_size:
                    size_exceeded = True
                    exceeded_size = total_size
                    break
                f.write(chunk)  # type: ignore[arg-type]

        # If size limit was exceeded, delete the partial upload and raise
        if size_exceeded and max_size is not None:
            try:
                blob.delete()
            except Exception:
                # Best effort cleanup - blob may not exist if upload failed early
                pass
            raise FileSizeLimitExceeded(max_size=max_size, actual_size=exceeded_size)

        return total_size
