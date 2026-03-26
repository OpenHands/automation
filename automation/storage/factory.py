import os

from automation.storage.file_store import FileStore


def get_file_store() -> FileStore:
    """
    Factory function to create the appropriate file store based on configuration.

    The FILE_STORE environment variable determines which backend to use:
    - "gcs" (default): Google Cloud Storage (GoogleCloudFileStore)
    - "s3": S3-compatible storage (S3FileStore) - works with AWS S3, MinIO, etc.

    Returns:
        A FileStore instance configured for the selected backend.

    Raises:
        ValueError: If FILE_STORE is set to an unsupported value.
    """
    file_store_type = os.environ.get("FILE_STORE", "gcs").lower()

    if file_store_type == "gcs":
        from automation.storage.google_cloud import GoogleCloudFileStore

        return GoogleCloudFileStore()
    elif file_store_type == "s3":
        from automation.storage.s3 import S3FileStore

        return S3FileStore()
    else:
        raise ValueError(
            f"Unsupported FILE_STORE type: {file_store_type}. "
            "Supported values: 'gcs', 's3'"
        )
