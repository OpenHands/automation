from abc import ABC, abstractmethod


class FileStore(ABC):
    """Abstract base class for file storage operations."""

    @abstractmethod
    def write(self, path: str, contents: str | bytes) -> None:
        """Write contents to a file at the given path."""
        pass

    @abstractmethod
    def read(self, path: str) -> str:
        """Read contents from a file at the given path."""
        pass

    @abstractmethod
    def list(self, path: str) -> list[str]:
        """List all files under the given path prefix."""
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete the file at the given path."""
        pass
