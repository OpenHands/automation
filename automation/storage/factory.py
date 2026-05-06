from automation.config import get_config
from automation.storage.file_store import FileStore


def get_file_store() -> FileStore:
    """
    Factory function to create the appropriate file store based on configuration.

    Configuration is read from StorageSettings (see automation/config.py).
    The FILE_STORE environment variable determines which backend to use:
    - "gcs" (default): Google Cloud Storage (GoogleCloudFileStore)
    - "s3": S3-compatible storage (S3FileStore) - works with AWS S3, MinIO, etc.
    - "local": Local filesystem (LocalFileStore) - for self-hosted deployments

    Returns:
        A FileStore instance configured for the selected backend.
    """
    storage = get_config().storage

    if storage.file_store == "gcs":
        from automation.storage.google_cloud import GoogleCloudFileStore

        return GoogleCloudFileStore(storage)
    elif storage.file_store == "s3":
        from automation.storage.s3 import S3FileStore

        return S3FileStore(storage)
    elif storage.file_store == "local":
        from automation.storage.local import LocalFileStore

        # local_storage_path is validated to be non-None by StorageSettings
        assert storage.local_storage_path is not None
        return LocalFileStore(storage.local_storage_path)
    else:
        # Unreachable due to Pydantic Literal validation, but explicit for safety
        raise ValueError(f"Unsupported file_store: {storage.file_store}")
