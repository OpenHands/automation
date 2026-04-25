from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import boto3
import botocore.exceptions

from automation.storage.file_store import FileStore
from automation.storage.google_cloud import BUCKET_PREFIX, FileSizeLimitExceeded


if TYPE_CHECKING:
    from automation.config import StorageSettings


logger = logging.getLogger(__name__)

# Default max size for streaming uploads (100MB)
# This prevents OOM when buffering chunks before upload
DEFAULT_MAX_STREAM_SIZE = 100 * 1024 * 1024


class S3FileStore(FileStore):
    """
    S3-compatible file store implementation.

    Supports AWS S3, MinIO, and other S3-compatible storage services.
    Configuration is provided via StorageSettings (see automation/config.py).

    Note: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are read directly by boto3
    from environment variables, following AWS SDK conventions.

    All files are stored under the "automation/" prefix in the bucket
    to isolate automation service data from other services.

    Note on streaming uploads:
        The write_stream method buffers data in memory before uploading.
        To prevent OOM, a default size limit of 100MB is enforced unless
        explicitly overridden. For larger files, consider using multipart
        upload directly or pre-staging to disk.
    """

    def __init__(self, settings: StorageSettings):
        """
        Initialize the S3 file store.

        Args:
            settings: StorageSettings instance with S3 configuration.
        """
        self.bucket_name = settings.aws_s3_bucket
        # Defensive: StorageSettings validates, but guard against direct instantiation
        if not self.bucket_name:
            raise ValueError("AWS_S3_BUCKET is required for S3 backend")

        # AWS credentials: Intentionally read directly from env vars, not from
        # StorageSettings. This follows AWS SDK conventions where boto3 also reads
        # these env vars (plus ~/.aws/credentials, IAM roles, etc.). Putting them
        # in StorageSettings would duplicate configuration and confuse users who
        # expect standard AWS credential chain behavior.
        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")

        secure = settings.aws_s3_secure
        endpoint = settings.aws_s3_endpoint

        # Validate endpoint scheme matches secure flag
        if endpoint:
            endpoint = self._validate_endpoint_scheme(secure, endpoint)

        self.client: Any = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint,
            use_ssl=secure,
        )

        # Auto-create bucket if explicitly enabled
        if settings.aws_s3_auto_create_bucket:
            self._ensure_bucket_exists()

    def _prefixed_path(self, path: str) -> str:
        """Add the automation prefix to a path."""
        # Remove leading slash if present
        path = path.lstrip("/")
        return f"{BUCKET_PREFIX}/{path}"

    def _ensure_bucket_exists(self) -> None:
        """Create the bucket if it doesn't exist.

        Only called when AWS_S3_AUTO_CREATE_BUCKET=true.
        Handles AlreadyOwnedByYou gracefully for idempotency.
        """
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            logger.debug(f"Bucket '{self.bucket_name}' already exists")
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ("404", "NoSuchBucket"):
                try:
                    self.client.create_bucket(Bucket=self.bucket_name)
                    logger.info(f"Created bucket '{self.bucket_name}'")
                except botocore.exceptions.ClientError as create_err:
                    # Handle race condition: bucket created between check and create
                    create_code = create_err.response.get("Error", {}).get("Code")
                    if create_code not in (
                        "BucketAlreadyOwnedByYou",
                        "BucketAlreadyExists",
                    ):
                        raise
            else:
                raise

    def _validate_endpoint_scheme(self, secure: bool, url: str) -> str:
        """Validate endpoint URL scheme matches security setting.

        Fails fast on misconfiguration rather than silently fixing it.

        Args:
            secure: Whether SSL/TLS is enabled.
            url: The endpoint URL.

        Returns:
            The validated URL (with scheme added if missing).

        Raises:
            ValueError: If the URL scheme conflicts with the secure setting.
        """
        if url.startswith("https://"):
            if not secure:
                raise ValueError(
                    f"HTTPS endpoint '{url}' conflicts with AWS_S3_SECURE=false. "
                    "Use http:// or set AWS_S3_SECURE=true."
                )
            return url
        elif url.startswith("http://"):
            if secure:
                raise ValueError(
                    f"HTTP endpoint '{url}' conflicts with AWS_S3_SECURE=true. "
                    "Use https:// or set AWS_S3_SECURE=false."
                )
            return url
        else:
            # No scheme provided, add based on secure flag
            scheme = "https://" if secure else "http://"
            return scheme + url

    def write(self, path: str, contents: str | bytes) -> None:
        """
        Write contents to a file at the given path.

        Args:
            path: The path/key in the bucket to write to (will be prefixed
                  with "automation/").
            contents: The content to write (string or bytes).
        """
        full_path = self._prefixed_path(path)
        as_bytes = contents.encode("utf-8") if isinstance(contents, str) else contents

        content_type = (
            "text/plain" if isinstance(contents, str) else "application/octet-stream"
        )

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=full_path,
                Body=as_bytes,
                ContentType=content_type,
            )
        except botocore.exceptions.ClientError as e:
            self._handle_client_error(e, "write", full_path)

    def read(self, path: str) -> bytes:
        """
        Read file contents from S3.

        Args:
            path: The path/key in the bucket (will be prefixed with "automation/").

        Returns:
            The file contents as bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        full_path = self._prefixed_path(path)
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=full_path)
            return response["Body"].read()
        except botocore.exceptions.ClientError as e:
            self._handle_client_error(e, "read", full_path)
            raise  # unreachable, but helps type checker

    def list(self, path: str) -> list[str]:
        """
        List all files under the given path prefix.

        Args:
            path: The prefix to search for (will be prefixed with "automation/").

        Returns:
            A list of file paths matching the prefix (without the "automation/"
            prefix).
        """
        full_path = self._prefixed_path(path)
        prefix_len = len(BUCKET_PREFIX) + 1  # +1 for the trailing slash

        try:
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name, Prefix=full_path
            )
            contents = response.get("Contents", [])
            # Strip the automation prefix from returned paths
            return [obj["Key"][prefix_len:] for obj in contents]
        except botocore.exceptions.ClientError as e:
            self._handle_client_error(e, "list", full_path)
            raise  # unreachable, but helps type checker

    def delete(self, path: str) -> None:
        """
        Delete the file at the given path.

        Args:
            path: The path/key in the bucket to delete (will be prefixed
                  with "automation/").

        Raises:
            FileNotFoundError: If the file doesn't exist or access is denied.
        """
        full_path = self._prefixed_path(path)
        try:
            # Check if key exists first (S3 delete doesn't error on missing keys)
            self.client.head_object(Bucket=self.bucket_name, Key=full_path)
            self.client.delete_object(Bucket=self.bucket_name, Key=full_path)
        except botocore.exceptions.ClientError as e:
            self._handle_client_error(e, "delete", full_path)

    def _handle_client_error(
        self, e: botocore.exceptions.ClientError, operation: str, path: str
    ) -> None:
        """Centralized error handling for S3 client errors.

        Logs full error details for debugging, then raises appropriate exception.
        Preserves the original exception as the cause for proper chaining.

        Args:
            e: The boto3 ClientError exception.
            operation: The operation being performed (read, write, delete, list).
            path: The S3 key/path involved.

        Raises:
            FileNotFoundError: For not-found and access errors (to match FileStore API).
        """
        error_code = e.response.get("Error", {}).get("Code")
        error_msg = e.response.get("Error", {}).get("Message", "")
        request_id = e.response.get("ResponseMetadata", {}).get("RequestId", "unknown")

        # Log full details for debugging
        logger.error(
            f"S3 {operation} failed: code={error_code}, msg={error_msg}, "
            f"bucket={self.bucket_name}, path={path}, request_id={request_id}"
        )

        # Map to FileNotFoundError to match FileStore interface
        if error_code in ("NoSuchBucket",):
            raise FileNotFoundError(
                f"Bucket '{self.bucket_name}' does not exist"
            ) from e
        elif error_code in ("404", "NoSuchKey"):
            raise FileNotFoundError(f"File not found: {path}") from e
        elif error_code == "AccessDenied":
            raise FileNotFoundError(
                f"Access denied to '{self.bucket_name}/{path}'"
            ) from e
        else:
            # For other errors, include the code in the message
            raise FileNotFoundError(
                f"S3 {operation} failed ({error_code}): {error_msg}"
            ) from e

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        """
        Stream content to a file, enforcing a size limit.

        Collects chunks from the async stream and uploads to S3. The upload
        is buffered in memory, so a size limit is enforced to prevent OOM.

        IMPORTANT: This method buffers the entire stream in memory before
        uploading. To prevent OOM issues:
        - Default max_size is 100MB if not specified
        - For larger files, use multipart upload directly or stage to disk

        Args:
            path: The path/key in the bucket to write to (will be prefixed
                  with "automation/").
            stream: An async iterator yielding bytes chunks.
            max_size: Maximum allowed file size in bytes. Defaults to 100MB
                      to prevent OOM. Set explicitly for different limits.
            content_type: MIME type for the uploaded file.

        Returns:
            The total number of bytes written.

        Raises:
            FileSizeLimitExceeded: If the stream exceeds max_size bytes.
        """
        # Apply default size limit to prevent OOM
        effective_max_size = (
            max_size if max_size is not None else DEFAULT_MAX_STREAM_SIZE
        )

        full_path = self._prefixed_path(path)
        chunks: list[bytes] = []
        total_size = 0

        async for chunk in stream:
            total_size += len(chunk)
            if total_size > effective_max_size:
                raise FileSizeLimitExceeded(
                    max_size=effective_max_size, actual_size=total_size
                )
            chunks.append(chunk)

        # Upload the collected data
        data = b"".join(chunks)
        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=full_path,
                Body=data,
                ContentType=content_type,
            )
        except botocore.exceptions.ClientError as e:
            self._handle_client_error(e, "write_stream", full_path)

        return total_size
