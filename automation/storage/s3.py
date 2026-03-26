import os
from collections.abc import AsyncIterator
from typing import Any

import boto3
import botocore.exceptions

from automation.storage.file_store import FileStore
from automation.storage.google_cloud import BUCKET_PREFIX, FileSizeLimitExceeded


class S3FileStore(FileStore):
    """
    S3-compatible file store implementation.

    Supports AWS S3, MinIO, and other S3-compatible storage services.
    Configure via environment variables:
    - AWS_ACCESS_KEY_ID: Access key
    - AWS_SECRET_ACCESS_KEY: Secret key
    - AWS_S3_ENDPOINT: Optional endpoint URL (for MinIO, LocalStack, etc.)
    - AWS_S3_BUCKET: Default bucket name
    - AWS_S3_SECURE: Whether to use HTTPS (default: true)

    All files are stored under the "automation/" prefix in the bucket
    to isolate automation service data from other services.
    """

    def __init__(self, bucket_name: str | None = None):
        """
        Initialize the S3 file store.

        Args:
            bucket_name: S3 bucket name. If not provided, reads from
                         AWS_S3_BUCKET environment variable.
        """
        self.bucket_name = bucket_name or os.environ.get("AWS_S3_BUCKET")
        if not self.bucket_name:
            raise ValueError(
                "Bucket name must be provided or AWS_S3_BUCKET env var must be set"
            )

        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        secure = os.environ.get("AWS_S3_SECURE", "true").lower() == "true"
        endpoint = self._ensure_url_scheme(secure, os.environ.get("AWS_S3_ENDPOINT"))

        self.client: Any = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint,
            use_ssl=secure,
        )

        # For non-AWS endpoints (MinIO, etc.): ensure bucket exists
        if endpoint:
            self._ensure_bucket_exists()

    def _prefixed_path(self, path: str) -> str:
        """Add the automation prefix to a path."""
        # Remove leading slash if present
        path = path.lstrip("/")
        return f"{BUCKET_PREFIX}/{path}"

    def _ensure_bucket_exists(self) -> None:
        """Create the bucket if it doesn't exist (for MinIO/emulator)."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ("404", "NoSuchBucket"):
                self.client.create_bucket(Bucket=self.bucket_name)
            else:
                raise

    def _ensure_url_scheme(self, secure: bool, url: str | None) -> str | None:
        """Ensure the URL has the correct scheme based on secure flag."""
        if not url:
            return None
        if secure:
            if not url.startswith("https://"):
                url = "https://" + url.removeprefix("http://")
        else:
            if not url.startswith("http://"):
                url = "http://" + url.removeprefix("https://")
        return url

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
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "AccessDenied":
                raise FileNotFoundError(f"Access denied to bucket '{self.bucket_name}'")
            elif error_code == "NoSuchBucket":
                raise FileNotFoundError(f"Bucket '{self.bucket_name}' does not exist")
            raise FileNotFoundError(
                f"Failed to write to '{self.bucket_name}/{full_path}': {e}"
            )

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
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchBucket":
                raise FileNotFoundError(f"Bucket '{self.bucket_name}' does not exist")
            elif error_code == "NoSuchKey":
                raise FileNotFoundError(f"File not found: {full_path}")
            raise FileNotFoundError(
                f"Failed to read from '{self.bucket_name}/{full_path}': {e}"
            )

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
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchBucket":
                raise FileNotFoundError(f"Bucket '{self.bucket_name}' does not exist")
            raise FileNotFoundError(
                f"Failed to list bucket '{self.bucket_name}' at path {full_path}: {e}"
            )

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
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchBucket":
                raise FileNotFoundError(f"Bucket '{self.bucket_name}' does not exist")
            elif error_code in ("404", "NoSuchKey"):
                raise FileNotFoundError(f"File not found: {full_path}")
            elif error_code == "AccessDenied":
                raise FileNotFoundError(f"Access denied to bucket '{self.bucket_name}'")
            raise FileNotFoundError(
                f"Failed to delete '{self.bucket_name}/{full_path}': {e}"
            )

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        """
        Stream content to a file, enforcing an optional size limit.

        Collects chunks from the async stream and uploads to S3. If max_size
        is specified and the total size exceeds it, the partial upload is
        deleted and FileSizeLimitExceeded is raised.

        Note: Unlike GCS which supports true streaming uploads, S3 requires
        multipart upload for streaming. For simplicity and compatibility with
        MinIO, this implementation buffers chunks and uses put_object.

        Args:
            path: The path/key in the bucket to write to (will be prefixed
                  with "automation/").
            stream: An async iterator yielding bytes chunks.
            max_size: Maximum allowed file size in bytes. If None, no limit.
            content_type: MIME type for the uploaded file.

        Returns:
            The total number of bytes written.

        Raises:
            FileSizeLimitExceeded: If the stream exceeds max_size bytes.
        """
        full_path = self._prefixed_path(path)
        chunks: list[bytes] = []
        total_size = 0
        size_exceeded = False
        exceeded_size = 0

        async for chunk in stream:
            total_size += len(chunk)
            if max_size is not None and total_size > max_size:
                size_exceeded = True
                exceeded_size = total_size
                break
            chunks.append(chunk)

        if size_exceeded and max_size is not None:
            raise FileSizeLimitExceeded(max_size=max_size, actual_size=exceeded_size)

        # Upload the collected data
        data = b"".join(chunks)
        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=full_path,
                Body=data,
                ContentType=content_type,
            )
        except Exception:
            # If upload fails, no cleanup needed (data wasn't written)
            raise

        return total_size
