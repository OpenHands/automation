"""Lightweight URL parsing utilities for tarball paths.

This module contains only pure functions with minimal dependencies,
making it safe to import in Temporal workflows (which run in a sandbox
that restricts certain imports like httpx, urllib.request, etc.).

For validation functions that require database access, see tarball_validation.py.
"""

import re
from uuid import UUID


# Valid external URL schemes (must be publicly accessible)
EXTERNAL_URL_SCHEMES = ("https://", "s3://", "gs://")

# HTTP(S) URL schemes that can be downloaded with curl inside a sandbox
HTTP_URL_SCHEMES = ("http://", "https://")

# Internal URL scheme for uploaded tarballs (must match config.INTERNAL_URL_SCHEME)
_INTERNAL_URL_SCHEME = "oh-internal"

# Internal URL prefix for uploaded tarballs
INTERNAL_URL_PREFIX = f"{_INTERNAL_URL_SCHEME}://uploads/"

# Compiled regex pattern for internal URLs: oh-internal://uploads/{uuid}
_INTERNAL_URL_PATTERN = re.compile(
    rf"^{re.escape(_INTERNAL_URL_SCHEME)}://uploads/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def get_internal_url_prefix() -> str:
    """Get the internal URL prefix (e.g., 'oh-internal://uploads/')."""
    return INTERNAL_URL_PREFIX


def build_internal_url(upload_id: UUID) -> str:
    """Build an internal URL for an upload."""
    return f"{INTERNAL_URL_PREFIX}{upload_id}"


def parse_internal_upload_id(tarball_path: str) -> UUID | None:
    """
    Extract upload_id from internal URL.

    Returns the UUID if the path matches the internal format,
    or None if it's not an internal URL.
    """
    match = _INTERNAL_URL_PATTERN.match(tarball_path)
    if match:
        return UUID(match.group(1))
    return None


def is_internal_url(tarball_path: str) -> bool:
    """Check if the tarball_path is an internal upload URL."""
    return tarball_path.startswith(f"{_INTERNAL_URL_SCHEME}://")


def is_valid_external_url(tarball_path: str) -> bool:
    """Check if the tarball_path has a valid external URL scheme."""
    return tarball_path.startswith(EXTERNAL_URL_SCHEMES)


def is_http_url(tarball_path: str) -> bool:
    """Check if the tarball_path is an HTTP(S) URL downloadable with curl."""
    return tarball_path.startswith(HTTP_URL_SCHEMES)
