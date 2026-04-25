from automation.config import get_storage_settings
from automation.storage.file_store import FileStore


def get_file_store() -> FileStore:
    """
    Factory function to create the appropriate file store based on configuration.

    Configuration is read from StorageSettings (see automation/config.py).
    The FILE_STORE environment variable determines which backend to use:
    - "gcs" (default): Google Cloud Storage (GoogleCloudFileStore)
    - "s3": S3-compatible storage (S3FileStore) - works with AWS S3, MinIO, etc.

    Returns:
        A FileStore instance configured for the selected backend.
    """
    storage = get_storage_settings()

    if storage.file_store == "gcs":
        from automation.storage.google_cloud import GoogleCloudFileStore

        return GoogleCloudFileStore(storage)
    else:  # s3
        from automation.storage.s3 import S3FileStore

        return S3FileStore(storage)
