from abc import ABC, abstractmethod


class FileStore(ABC):
    """Abstract base class for file storage operations.

    Note: This interface is currently upload-only. Read methods are not included
    because this service only handles file uploads. Files are read by other
    services (e.g., the dispatcher) that access storage directly. This may
    change in the future if read functionality is needed.
    """

    @abstractmethod
    def write(self, path: str, contents: str | bytes) -> None:
        """Write contents to a file at the given path."""
        pass

    @abstractmethod
    def list(self, path: str) -> list[str]:
        """List all files under the given path prefix."""
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete the file at the given path."""
        pass
