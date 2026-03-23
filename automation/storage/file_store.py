from abc import ABC, abstractmethod


class FileStore(ABC):
    """Abstract base class for file storage operations."""

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
