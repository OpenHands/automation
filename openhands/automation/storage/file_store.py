from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


# All files are stored under this prefix in the bucket to isolate
# automation service data from other services.
BUCKET_PREFIX = "automation"


class FileStore(ABC):
    """Abstract base class for file storage operations."""

    def _prefixed_path(self, path: str) -> str:
        """Add the automation prefix to a path."""
        path = path.lstrip("/")
        return f"{BUCKET_PREFIX}/{path}"

    @abstractmethod
    def write(self, path: str, contents: str | bytes) -> None:
        """Write contents to a file at the given path."""
        pass

    @abstractmethod
    def read(self, path: str) -> bytes:
        """Read and return the contents of the file at the given path.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        pass

    @abstractmethod
    def list(self, path: str) -> list[str]:
        """List all files under the given path prefix."""
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete the file at the given path."""
        pass

    @abstractmethod
    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        """Stream content to a file, enforcing an optional size limit.

        Args:
            path: The path/key to write to.
            stream: An async iterator yielding bytes chunks.
            max_size: Maximum allowed file size in bytes. If None, no limit.
            content_type: MIME type for the uploaded file.

        Returns:
            The total number of bytes written.

        Raises:
            FileSizeLimitExceeded: If the stream exceeds max_size bytes.
        """
        pass
