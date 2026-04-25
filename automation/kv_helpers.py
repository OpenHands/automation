"""Helper functions for KV store operations.

Provides utilities for:
- Parsing and manipulating nested paths in JSON values
- Safe encryption with proper HTTP error handling
"""

import logging
from typing import Any

from fastapi import HTTPException, status

from automation.utils.kv import (
    KVEncryptionError,
    KVValueError,
    encrypt_value,
)


logger = logging.getLogger(__name__)


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
        ValueError: If path has invalid syntax (e.g., unclosed bracket).
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
