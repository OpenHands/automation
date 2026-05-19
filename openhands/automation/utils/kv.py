"""KV store utilities: JWT tokens, JSON validation, and value encryption.

This module provides:
- JWT token generation/verification for KV store authentication
- Strict-JSON validation (rejects NaN/Infinity, caps nesting depth)
- Encryption/decryption of KV state via the SDK's :class:`Cipher`

The :class:`Cipher` helper from ``openhands.sdk.utils.cipher`` wraps Fernet
(AES-128-CBC + HMAC-SHA256). It derives a 256-bit key from the configured
service secret, generates a fresh IV per encryption, and authenticates the
ciphertext, which is everything we need for protecting per-automation state
at rest. We deliberately use the SDK's Cipher instead of rolling our own AES
to keep this module small and to share a battle-tested implementation with
the rest of the platform.

Fernet emits a URL-safe base64 string ("token") rather than raw bytes, so KV
state is stored in a text column. The ~33% base64 overhead is acceptable for
small automation state (counters, cursors, configs) and keeps the schema
simple.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pydantic import SecretStr

from openhands.sdk.utils.cipher import Cipher


class KVTokenError(Exception):
    """Error with KV store JWT token."""


class KVEncryptionError(Exception):
    """Error with KV value encryption/decryption."""


class KVValueError(Exception):
    """Error with KV value format or content."""


# Maximum nesting depth for JSON values.
# Prevents stack overflow from deeply nested structures and limits complexity.
# 32 levels is generous (most real configs are <10 levels deep).
_MAX_NESTING_DEPTH = 32

# Token expiration: 24 hours
#
# Intentionally longer than the max automation run time (currently 2 hours)
# to provide margin for long-running automations, cleanup operations after
# run completion, and clock skew between services. The token only grants
# access to its specific automation's KV data, so a longer validity window
# has minimal security impact.
KV_TOKEN_EXPIRATION_HOURS = 24


# --- JWT Token Functions ---


class KVTokenClaims:
    """Verified claims from a KV store JWT token."""

    __slots__ = ("automation_id",)

    def __init__(self, automation_id: uuid.UUID):
        self.automation_id = automation_id


def create_kv_token(
    secret: str,
    automation_id: uuid.UUID,
    run_id: uuid.UUID,
) -> str:
    """Create a JWT token for KV store access.

    The token embeds the automation_id as a trusted claim, ensuring that
    KV operations are scoped to the correct automation.

    Args:
        secret: The signing secret (AUTOMATION_KV_SECRET)
        automation_id: UUID of the automation
        run_id: UUID of the current run (for audit)

    Returns:
        Signed JWT token string
    """
    now = datetime.now(UTC)
    payload = {
        "automation_id": str(automation_id),
        "run_id": str(run_id),
        "iat": now,
        "exp": now + timedelta(hours=KV_TOKEN_EXPIRATION_HOURS),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_kv_token(secret: str, token: str) -> KVTokenClaims:
    """Verify a KV store JWT token and extract claims.

    Args:
        secret: The signing secret (AUTOMATION_KV_SECRET)
        token: The JWT token to verify

    Returns:
        KVTokenClaims with automation_id

    Raises:
        KVTokenError: If token is invalid, expired, or malformed
    """
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        automation_id_str = payload.get("automation_id")
        if not automation_id_str:
            raise KVTokenError("Token missing automation_id claim")
        return KVTokenClaims(automation_id=uuid.UUID(automation_id_str))
    except jwt.ExpiredSignatureError:
        raise KVTokenError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise KVTokenError(f"Invalid token: {e}")
    except ValueError as e:
        raise KVTokenError(f"Invalid automation_id format: {e}")


# --- JSON Validation ---


def _check_nesting_depth(value: Any, current_depth: int = 0) -> None:
    """Check that a value doesn't exceed maximum nesting depth.

    Raises:
        KVValueError: If nesting exceeds _MAX_NESTING_DEPTH
    """
    if current_depth > _MAX_NESTING_DEPTH:
        raise KVValueError(
            f"Value exceeds maximum nesting depth of {_MAX_NESTING_DEPTH}"
        )

    if isinstance(value, dict):
        for v in value.values():
            _check_nesting_depth(v, current_depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_nesting_depth(item, current_depth + 1)


def _validate_json_value(value: Any) -> str:
    """Validate and serialize a value to strict JSON.

    Ensures the value is JSON-serializable, contains only standard JSON
    types (rejects NaN/Infinity), and doesn't exceed maximum nesting depth.

    Raises:
        KVValueError: If value is not valid strict JSON
    """
    try:
        _check_nesting_depth(value)
    except RecursionError:
        raise KVValueError(
            f"Value exceeds maximum nesting depth of {_MAX_NESTING_DEPTH}"
        )

    # Strict JSON: allow_nan=False rejects NaN/Infinity, ensure_ascii=False
    # allows UTF-8 (more compact, widely supported).
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False)
    except ValueError as e:
        raise KVValueError(f"Value contains non-JSON-compliant data: {e}")
    except TypeError as e:
        raise KVValueError(f"Value is not JSON-serializable: {e}")


# --- Encryption Functions ---


def encrypt_value(secret: str, value: Any) -> str:
    """Encrypt a value for storage using the SDK Cipher (Fernet).

    Validates and JSON-serializes the value, then returns a base64-encoded
    Fernet token suitable for storage in a TEXT column.

    Raises:
        KVValueError: If value is not valid strict JSON
        KVEncryptionError: If encryption fails
    """
    plaintext_str = _validate_json_value(value)
    try:
        ciphertext = Cipher(secret).encrypt(SecretStr(plaintext_str))
    except Exception as e:
        raise KVEncryptionError(f"Failed to encrypt value: {e}")
    assert ciphertext is not None  # SecretStr is non-None, so result is non-None
    return ciphertext


def decrypt_value(secret: str, encrypted: str) -> Any:
    """Decrypt a value previously produced by :func:`encrypt_value`.

    Returns the parsed JSON object.

    Raises:
        KVEncryptionError: If decryption fails (wrong key, tampered data, etc.)
    """
    try:
        plaintext_secret = Cipher(secret).decrypt(encrypted)
    except Exception as e:
        raise KVEncryptionError(f"Failed to decrypt value: {e}")

    if plaintext_secret is None:
        # Cipher.decrypt returns None on InvalidToken; surface as an explicit
        # encryption error so callers can map it to HTTP 500.
        raise KVEncryptionError("Failed to decrypt value: invalid token")

    try:
        return json.loads(plaintext_secret.get_secret_value())
    except json.JSONDecodeError as e:
        raise KVEncryptionError(f"Decrypted value is not valid JSON: {e}")
