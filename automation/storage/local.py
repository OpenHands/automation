"""Local filesystem storage backend for self-hosted deployments."""

import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from automation.storage.file_store import FileStore
from automation.storage.google_cloud import FileSizeLimitExceeded

logger = logging.getLogger("automation.storage.local")


class LocalFileStore(FileStore):
    """File storage backed by the local filesystem.

    Used for self-hosted/local deployments where cloud storage isn't available.
    Stores files under a configurable base directory.
    """

    def __init__(self, base_path: str | Path):
        """Initialize local file store.

        Args:
            base_path: Base directory for storing files.
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info("LocalFileStore initialized at %s", self.base_path)

    def _full_path(self, path: str) -> Path:
        """Get the full filesystem path for a storage path."""
        prefixed = self._prefixed_path(path)
        return self.base_path / prefixed

    def write(self, path: str, contents: str | bytes) -> None:
        """Write contents to a file at the given path."""
        full_path = self._full_path(path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(contents, str):
            contents = contents.encode("utf-8")

        full_path.write_bytes(contents)
        logger.debug("Wrote %d bytes to %s", len(contents), path)

    def read(self, path: str) -> bytes:
        """Read and return the contents of the file at the given path."""
        full_path = self._full_path(path)
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return full_path.read_bytes()

    def list(self, path: str) -> list[str]:
        """List all files under the given path prefix."""
        full_path = self._full_path(path)
        if not full_path.exists():
            return []

        # If it's a file, return just that file
        if full_path.is_file():
            return [path]

        # If it's a directory, list all files recursively
        result = []
        prefix_len = len(str(self.base_path)) + 1  # +1 for trailing slash
        for root, _, files in os.walk(full_path):
            for f in files:
                file_path = os.path.join(root, f)
                # Return path relative to base_path, without the automation/ prefix
                rel_path = file_path[prefix_len:]
                # Remove the BUCKET_PREFIX from the start
                if rel_path.startswith("automation/"):
                    rel_path = rel_path[len("automation/") :]
                result.append(rel_path)
        return result

    def delete(self, path: str) -> None:
        """Delete the file at the given path."""
        full_path = self._full_path(path)
        if full_path.exists():
            if full_path.is_file():
                full_path.unlink()
                logger.debug("Deleted file %s", path)
            else:
                # Delete directory and all contents
                import shutil

                shutil.rmtree(full_path)
                logger.debug("Deleted directory %s", path)

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        """Stream content to a file, enforcing an optional size limit."""
        full_path = self._full_path(path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        total_bytes = 0
        with full_path.open("wb") as f:
            async for chunk in stream:
                if max_size is not None and total_bytes + len(chunk) > max_size:
                    # Clean up partial file
                    f.close()
                    full_path.unlink(missing_ok=True)
                    raise FileSizeLimitExceeded(
                        f"File exceeds maximum size of {max_size} bytes"
                    )
                f.write(chunk)
                total_bytes += len(chunk)

        logger.debug("Streamed %d bytes to %s", total_bytes, path)
        return total_bytes
