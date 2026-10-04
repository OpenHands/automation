from openhands.automation.config import get_config
from openhands.automation.storage.file_store import FileStore


def get_file_store() -> FileStore:
    """
    Factory function to create the appropriate file store based on configuration.

    Configuration is read from StorageSettings (see automation/config.py).
    The FILE_STORE environment variable determines which backend to use:
    - "local" (default): Local filesystem (LocalFileStore) - for self-hosted deployments
    - "gcs": Google Cloud Storage (GoogleCloudFileStore)
    - "s3": S3-compatible storage (S3FileStore) - works with AWS S3, MinIO, etc.

    Returns:
        A FileStore instance configured for the selected backend.
    """
    storage = get_config().storage

    if storage.file_store == "gcs":
        from openhands.automation.storage.google_cloud import GoogleCloudFileStore

        return GoogleCloudFileStore(storage)
    elif storage.file_store == "s3":
        from openhands.automation.storage.s3 import S3FileStore

        return S3FileStore(storage)
    elif storage.file_store == "local":
        from openhands.automation.storage.local import LocalFileStore

        # local_storage_path is a Path, validated to be non-empty by StorageSettings
        return LocalFileStore(storage.local_storage_path.expanduser())
    else:
        # Unreachable due to Pydantic Literal validation, but explicit for safety
        raise ValueError(f"Unsupported file_store: {storage.file_store}")
