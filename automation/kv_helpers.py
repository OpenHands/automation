"""Helper functions for KV store operations.

Provides utilities for:
- Parsing and manipulating nested paths in JSON values
- Safe encryption/decryption with proper HTTP error handling
- Type validation helpers for KV values
- Key name validation
"""

import logging
from typing import Any

from fastapi import HTTPException, status

from automation.utils.kv import (
    KVEncryptionError,
    KVValueError,
    decrypt_value,
    encrypt_value,
)


logger = logging.getLogger(__name__)


# Maximum key length (matches database column constraint)
_MAX_KEY_LENGTH = 255

# Maximum path depth (matches value nesting depth limit)
_MAX_PATH_DEPTH = 32


# --- Key Validation ---


def validate_key(key: str) -> str:
    """Validate a KV key name for safe storage and retrieval.

    Keys are validated to ensure they:
    - Are not empty or whitespace-only
    - Don't start with '$' (reserved for system keys like $version)
    - Don't exceed the database column length limit (255 chars)
    - Don't contain control characters (which could cause issues in logs, URLs, etc.)

    Args:
        key: The key name to validate

    Returns:
        The validated key (unmodified if valid)

    Raises:
        HTTPException: 400 Bad Request with descriptive error if validation fails
    """
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_key: key cannot be empty",
        )

    if not key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_key: key cannot be whitespace-only",
        )

    # Reserve $ prefix for system keys ($version, future meta keys)
    if key.startswith("$"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_key: keys starting with '$' are reserved for system use",
        )

    if len(key) > _MAX_KEY_LENGTH:
        msg = f"invalid_key: key exceeds {_MAX_KEY_LENGTH} chars ({len(key)} given)"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    # Check for control characters (ASCII 0-31 and 127)
    # These can cause issues in logging, URLs, and debugging
    for i, char in enumerate(key):
        code = ord(char)
        if code < 32 or code == 127:
            char_repr = f"\\x{code:02x}" if code < 32 else "\\x7f"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid_key: control character {char_repr} at position {i}",
            )

    return key


# --- HTTP Error Helpers ---


def safe_encrypt(secret: str, value: Any) -> bytes:
    """Encrypt a value with proper HTTP error handling.

    Wraps encrypt_value() to convert exceptions to appropriate HTTP errors:
    - KVValueError (invalid JSON) → 400 Bad Request
    - KVEncryptionError (encryption failure) → 500 Internal Server Error

    JSON Validation:
        Values are validated before encryption to ensure they are strict JSON:
        - NaN, Infinity, -Infinity are rejected (not valid JSON)
        - Maximum nesting depth is enforced (prevents DoS)
        - Non-serializable types are rejected

    Args:
        secret: The encryption secret
        value: Any JSON-serializable value

    Returns:
        Encrypted bytes

    Raises:
        HTTPException: 400 for invalid values, 500 for encryption errors
    """
    try:
        return encrypt_value(secret, value)
    except KVValueError as e:
        # Client's fault: invalid JSON value (NaN, too deep, non-serializable)
        logger.warning("Invalid KV value rejected: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_value: {e}",
        )
    except KVEncryptionError as e:
        # Our fault: encryption failed unexpectedly
        logger.error("Failed to encrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt value",
        )


def safe_decrypt(secret: str, encrypted: bytes) -> Any:
    """Decrypt a value with proper HTTP error handling.

    Wraps decrypt_value() to convert KVEncryptionError to HTTP 500.

    Args:
        secret: The encryption secret
        encrypted: Encrypted bytes from the database

    Returns:
        The decrypted JSON value

    Raises:
        HTTPException: 500 for decryption errors
    """
    try:
        return decrypt_value(secret, encrypted)
    except KVEncryptionError as e:
        # Our fault: decryption failed (corrupted data, wrong key, etc.)
        logger.error("Failed to decrypt KV value: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt value",
        )


def require_dict(value: Any) -> dict:
    """Validate that a value is a dict, raising HTTP 400 if not.

    Args:
        value: The value to check

    Returns:
        The value (for chaining)

    Raises:
        HTTPException: 400 if value is not a dict
    """
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not an object",
        )
    return value


def require_list(value: Any) -> list:
    """Validate that a value is a list, raising HTTP 400 if not.

    Args:
        value: The value to check

    Returns:
        The value (for chaining)

    Raises:
        HTTPException: 400 if value is not a list
    """
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not a list",
        )
    return value


def require_numeric(value: Any) -> int | float:
    """Validate that a value is numeric (int or float), raising HTTP 400 if not.

    Note: Booleans are explicitly rejected even though bool is a subclass of int
    in Python. This prevents confusing behavior where True becomes 2 after increment.

    Args:
        value: The value to check

    Returns:
        The value (for chaining)

    Raises:
        HTTPException: 400 if value is not numeric (or is a boolean)
    """
    # Explicitly reject booleans (bool is subclass of int in Python)
    if isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is boolean, not numeric",
        )
    if not isinstance(value, (int, float)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not numeric",
        )
    return value


def require_int(value: Any) -> int:
    """Validate that a value is an integer, raising HTTP 400 if not.

    This is stricter than require_numeric - it rejects floats.
    Used for operations like incr/decr where float arithmetic could
    cause unexpected precision loss.

    Note: Booleans are explicitly rejected even though bool is a subclass of int
    in Python. This prevents confusing behavior where True becomes 2 after increment.

    Args:
        value: The value to check

    Returns:
        The value (for chaining)

    Raises:
        HTTPException: 400 if value is not an integer
    """
    # Explicitly reject booleans (bool is subclass of int in Python)
    if isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is boolean, not integer",
        )
    if not isinstance(value, int):
        if isinstance(value, float):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="type_mismatch: value is float, not integer (integer required)",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type_mismatch: value is not an integer",
        )
    return value


# --- Path Operations ---


def parse_path(path: str) -> list[str]:
    """Parse a path string into parts.

    Supports:
    - Dot notation: database.host
    - Bracket notation: config["my.key.with.dots"]

    Args:
        path: A dot-notation or bracket-notation path string.

    Returns:
        List of path segments.

    Raises:
        ValueError: If path has invalid syntax (e.g., unclosed bracket) or
                   exceeds maximum depth (_MAX_PATH_DEPTH).
    """
    parts: list[str] = []
    current = ""
    i = 0

    while i < len(path):
        char = path[i]

        if char == ".":
            if current:
                parts.append(current)
                current = ""
        elif char == "[":
            if current:
                parts.append(current)
                current = ""
            # Find closing bracket
            end = path.find("]", i)
            if end == -1:
                raise ValueError(f"Invalid path: unclosed bracket in '{path}'")
            # Extract key (strip quotes if present)
            key = path[i + 1 : end]
            if key.startswith('"') and key.endswith('"'):
                key = key[1:-1]
            elif key.startswith("'") and key.endswith("'"):
                key = key[1:-1]
            parts.append(key)
            i = end
        else:
            current += char

        i += 1

    if current:
        parts.append(current)

    # Enforce path depth limit to prevent DoS via deeply nested paths
    if len(parts) > _MAX_PATH_DEPTH:
        raise ValueError(
            f"Path exceeds maximum depth of {_MAX_PATH_DEPTH} ({len(parts)} segments)"
        )

    return parts


def get_nested_value(obj: Any, path: str) -> Any:
    """Get a value at a nested path using dot notation.

    Supports bracket notation for keys with dots: config["my.key"]

    Args:
        obj: The object to traverse (dict or list).
        path: Dot-notation or bracket-notation path.

    Returns:
        The value at the specified path.

    Raises:
        KeyError: If path does not exist in the object.
    """
    if not path:
        return obj

    parts = parse_path(path)
    current = obj

    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"Path '{path}' not found")
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                raise KeyError(f"Path '{path}' not found")
        else:
            raise KeyError(f"Path '{path}' not found")

    return current


def set_nested_value(obj: dict, path: str, value: Any) -> dict:
    """Set a value at a nested path using dot notation.

    Creates intermediate dicts as needed.

    Args:
        obj: The dict to modify.
        path: Dot-notation or bracket-notation path.
        value: The value to set at the path.

    Returns:
        The modified dict (same reference as input).

    Raises:
        ValueError: If intermediate path element is not a dict.
    """
    parts = parse_path(path)
    current = obj

    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]
        if not isinstance(current, dict):
            raise ValueError(
                f"Cannot set path '{path}': intermediate value is not a dict"
            )

    current[parts[-1]] = value
    return obj
