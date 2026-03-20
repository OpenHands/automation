import os

from google.cloud import storage

from automation.storage.file_store import FileStore


class GoogleCloudFileStore(FileStore):
    """
    Google Cloud Storage file store implementation.

    Supports both real GCS and fake-gcs-server emulator.
    When STORAGE_EMULATOR_HOST environment variable is set, the client
    automatically connects to the emulator instead of real GCS.
    """

    def __init__(self, bucket_name: str | None = None):
        """
        Initialize the Google Cloud file store.

        Args:
            bucket_name: GCS bucket name. If not provided, reads from
                         GCS_BUCKET_NAME environment variable.
        """
        self.bucket_name = bucket_name or os.environ.get("GCS_BUCKET_NAME")
        if not self.bucket_name:
            raise ValueError(
                "Bucket name must be provided or GCS_BUCKET_NAME env var must be set"
            )

        self._client: storage.Client | None = None
        self._bucket: storage.Bucket | None = None

    @property
    def client(self) -> storage.Client:
        """Lazily initialize and return the GCS client."""
        if self._client is None:
            # When STORAGE_EMULATOR_HOST is set, the client automatically
            # connects to the emulator (e.g., fake-gcs-server)
            self._client = storage.Client()
        return self._client

    @property
    def bucket(self) -> storage.Bucket:
        """Lazily initialize and return the GCS bucket."""
        if self._bucket is None:
            self._bucket = self.client.bucket(self.bucket_name)
            # For emulator: ensure bucket exists
            if os.environ.get("STORAGE_EMULATOR_HOST"):
                self._ensure_bucket_exists()
        return self._bucket

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
            path: The path/key in the bucket to write to.
            contents: The content to write (string or bytes).
        """
        blob = self.bucket.blob(path)
        if isinstance(contents, str):
            blob.upload_from_string(contents, content_type="text/plain")
        else:
            blob.upload_from_string(contents, content_type="application/octet-stream")

    def read(self, path: str) -> str:
        """
        Read contents from a file at the given path.

        Args:
            path: The path/key in the bucket to read from.

        Returns:
            The file contents as a string.

        Raises:
            google.cloud.exceptions.NotFound: If the file doesn't exist.
        """
        blob = self.bucket.blob(path)
        return blob.download_as_text()

    def list(self, path: str) -> list[str]:
        """
        List all files under the given path prefix.

        Args:
            path: The prefix to search for.

        Returns:
            A list of file paths matching the prefix.
        """
        blobs = self.client.list_blobs(self.bucket_name, prefix=path)
        return [blob.name for blob in blobs]

    def delete(self, path: str) -> None:
        """
        Delete the file at the given path.

        Args:
            path: The path/key in the bucket to delete.

        Raises:
            google.cloud.exceptions.NotFound: If the file doesn't exist.
        """
        blob = self.bucket.blob(path)
        blob.delete()
