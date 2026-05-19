"""KV store utilities: JWT tokens and AES-256-GCM encryption.

This module provides:
- JWT token generation/verification for KV store authentication
- AES-256-GCM encryption/decryption for KV values

All KV values are encrypted at the application level before storage.
JWT tokens are scoped per-automation run with short expiration.


Encryption Design Decisions
===========================

We evaluated several approaches for encrypting KV store values:

1. JWE (JSON Web Encryption) with TEXT column
   - Pros: Standard format, self-describing (includes algorithm headers)
   - Cons: Base64 encoding adds ~33% overhead, JWE headers add ~70 bytes
   - Storage: 14-byte plaintext → 100 bytes stored (7x overhead for small values)

2. AES-256-GCM with TEXT column (base64-encoded)
   - Pros: Simpler than JWE, widely supported
   - Cons: Still has ~33% base64 overhead
   - Storage: 14-byte plaintext → ~60 bytes stored

3. AES-256-GCM with BYTEA column (raw bytes) ← CHOSEN
   - Pros: Minimal overhead (28 bytes fixed), efficient binary storage
   - Cons: Not self-describing (but we only use one algorithm anyway)
   - Storage: 14-byte plaintext → 42 bytes stored (28-byte fixed overhead)

We chose option 3 because:
- KV stores typically have many small values (counters, flags, small configs)
- The 28-byte fixed overhead (12-byte nonce + 16-byte auth tag) is acceptable
- For larger values, overhead approaches 0% (vs 33% for base64)
- BYTEA is the natural PostgreSQL type for binary data
- PostgreSQL TOAST handles binary data efficiently


Why Not JSONB?
--------------

PostgreSQL's JSONB type offers efficient JSON storage with indexing and query
capabilities. However, we can't use it because:

1. We encrypt values at the application layer before storage
2. Encrypted data is opaque binary, not valid JSON
3. The ciphertext cannot be queried or indexed anyway

If queryable JSON were needed, we'd have to either:
- Skip encryption (unacceptable for sensitive automation state)
- Use PostgreSQL Transparent Data Encryption (TDE) for at-rest encryption
- Use pgcrypto for column-level encryption (but then values are still opaque)

Since automation state may contain secrets, API keys, or sensitive config,
application-level encryption is the right choice despite losing JSONB benefits.


PostgreSQL Storage Considerations
=================================

PostgreSQL uses TOAST (The Oversized-Attribute Storage Technique) for large values:
- Values < 2KB: Stored inline (optimal performance)
- Values 2-8KB: Compressed inline (~2x slower due to compression CPU)
- Values > 8KB: Stored in separate TOAST table (~5x slower, chunked storage)

For a KV store used for automation state:
- Most values should be small (counters, flags, configs) → under 2KB
- Default 64KB limit allows occasional larger blobs
- Values approaching the limit will use TOAST chunked storage


Key Derivation
==============

The encryption key is derived from AUTOMATION_KV_SECRET by:
1. UTF-8 encoding the secret string
2. Taking the first 32 bytes (truncating if longer)
3. Padding with null bytes if shorter than 32 bytes

This is simple but adequate for our use case where:
- The secret is configured by operators (not user-supplied)
- Key rotation requires re-encryption of all values anyway

For a more robust approach, consider HKDF or Argon2 key derivation.
This is noted as a potential future improvement.


Wire Format
===========

Encrypted values are stored as: nonce || ciphertext || tag

    +------------+------------------+------------+
    | 12 bytes   | variable length  | 16 bytes   |
    | nonce/IV   | ciphertext       | auth tag   |
    +------------+------------------+------------+

- Nonce: Random 96-bit IV, generated fresh for each encryption
- Ciphertext: AES-256-GCM encrypted JSON bytes
- Auth tag: 128-bit authentication tag (integrity protection)

Total overhead: 28 bytes (fixed, regardless of plaintext size)
"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KVTokenError(Exception):
    """Error with KV store JWT token."""

    pass


class KVEncryptionError(Exception):
    """Error with KV value encryption/decryption."""

    pass


# --- Constants ---

# Nonce size for AES-GCM (96 bits = 12 bytes, as recommended by NIST)
_NONCE_SIZE = 12

# Auth tag size for AES-GCM (128 bits = 16 bytes)
_TAG_SIZE = 16

# AES-256 key size (256 bits = 32 bytes)
_KEY_SIZE = 32

# Maximum nesting depth for JSON values.
# Prevents stack overflow from deeply nested structures and limits complexity.
# 32 levels is generous (most real configs are <10 levels deep).
_MAX_NESTING_DEPTH = 32

# Token expiration: 24 hours
#
# This is intentionally longer than the max automation run time (currently 2 hours)
# to provide margin for:
# 1. Long-running automations that approach the timeout limit
# 2. Any cleanup operations that need KV access after run completion
# 3. Clock skew between services
#
# The token is only usable to access the specific automation's KV data,
# so a longer validity window has minimal security impact.
KV_TOKEN_EXPIRATION_HOURS = 24


# --- JWT Token Functions ---

# Default lock timeout in milliseconds (matches Automation model default)
DEFAULT_LOCK_TIMEOUT_MS = 5000


class KVTokenClaims:
    """Verified claims from a KV store JWT token."""

    __slots__ = ("automation_id", "lock_timeout_ms")

    def __init__(self, automation_id: uuid.UUID, lock_timeout_ms: int):
        self.automation_id = automation_id
        self.lock_timeout_ms = lock_timeout_ms


def create_kv_token(
    secret: str,
    automation_id: uuid.UUID,
    run_id: uuid.UUID,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
) -> str:
    """Create a JWT token for KV store access.

    The token embeds the automation_id and lock_timeout_ms as trusted claims,
    ensuring that KV operations are scoped to the correct automation with
    the configured timeout.

    Args:
        secret: The signing secret (AUTOMATION_KV_SECRET)
        automation_id: UUID of the automation
        run_id: UUID of the current run (for audit)
        lock_timeout_ms: Lock timeout in milliseconds (from automation config)

    Returns:
        Signed JWT token string
    """
    now = datetime.now(UTC)
    payload = {
        "automation_id": str(automation_id),
        "run_id": str(run_id),
        "lock_timeout_ms": lock_timeout_ms,
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
        KVTokenClaims with automation_id and lock_timeout_ms

    Raises:
        KVTokenError: If token is invalid, expired, or malformed
    """
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        automation_id_str = payload.get("automation_id")
        if not automation_id_str:
            raise KVTokenError("Token missing automation_id claim")

        # lock_timeout_ms is optional for backward compatibility with old tokens
        lock_timeout_ms = payload.get("lock_timeout_ms", DEFAULT_LOCK_TIMEOUT_MS)
        if not isinstance(lock_timeout_ms, int) or lock_timeout_ms < 100:
            lock_timeout_ms = DEFAULT_LOCK_TIMEOUT_MS

        return KVTokenClaims(
            automation_id=uuid.UUID(automation_id_str),
            lock_timeout_ms=lock_timeout_ms,
        )
    except jwt.ExpiredSignatureError:
        raise KVTokenError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise KVTokenError(f"Invalid token: {e}")
    except ValueError as e:
        raise KVTokenError(f"Invalid automation_id format: {e}")


# --- JSON Validation ---


class KVValueError(Exception):
    """Error with KV value format or content."""

    pass


def _check_nesting_depth(value: Any, current_depth: int = 0) -> None:
    """Check that a value doesn't exceed maximum nesting depth.

    Args:
        value: The value to check
        current_depth: Current recursion depth

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

    Ensures the value:
    1. Is JSON-serializable
    2. Contains only standard JSON types (rejects NaN, Infinity)
    3. Doesn't exceed maximum nesting depth

    Args:
        value: Any JSON-serializable value

    Returns:
        JSON string representation

    Raises:
        KVValueError: If value is not valid strict JSON
    """
    # Check nesting depth first (before json.dumps which could stack overflow)
    try:
        _check_nesting_depth(value)
    except RecursionError:
        raise KVValueError(
            f"Value exceeds maximum nesting depth of {_MAX_NESTING_DEPTH}"
        )

    # Serialize with strict settings:
    # - allow_nan=False: Reject NaN, Infinity, -Infinity (not valid JSON)
    # - ensure_ascii=False: Allow UTF-8 (more compact, widely supported)
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False)
    except ValueError as e:
        # ValueError from allow_nan=False when value contains NaN/Infinity
        raise KVValueError(f"Value contains non-JSON-compliant data: {e}")
    except TypeError as e:
        # TypeError when value contains non-serializable types
        raise KVValueError(f"Value is not JSON-serializable: {e}")


# --- Encryption Functions ---


def _derive_key(secret: str) -> bytes:
    """Derive a 256-bit AES key from the secret string.

    Uses simple truncation/padding. See module docstring for rationale
    and notes on potential HKDF improvement.

    Args:
        secret: The encryption secret (AUTOMATION_KV_SECRET)

    Returns:
        32-byte key suitable for AES-256
    """
    key_bytes = secret.encode("utf-8")
    if len(key_bytes) >= _KEY_SIZE:
        return key_bytes[:_KEY_SIZE]
    else:
        return key_bytes.ljust(_KEY_SIZE, b"\0")


def encrypt_value(secret: str, value: Any) -> bytes:
    """Encrypt a value for storage using AES-256-GCM.

    The value is validated, JSON-serialized, then encrypted. The result is
    raw bytes suitable for storage in a BYTEA column.

    Validation ensures:
    - Value is JSON-serializable
    - No NaN, Infinity, or other non-standard JSON values
    - Nesting depth doesn't exceed _MAX_NESTING_DEPTH (32 levels)

    Wire format: nonce (12 bytes) || ciphertext || auth_tag (16 bytes)

    Args:
        secret: The encryption secret (AUTOMATION_KV_SECRET)
        value: Any JSON-serializable value

    Returns:
        Encrypted bytes (nonce + ciphertext + tag)

    Raises:
        KVValueError: If value is not valid strict JSON
        KVEncryptionError: If encryption fails
    """
    # Validate and serialize to strict JSON
    # This raises KVValueError for invalid values (NaN, too deep, etc.)
    plaintext_str = _validate_json_value(value)
    plaintext = plaintext_str.encode("utf-8")

    try:
        # Generate random nonce (critical: must be unique per encryption)
        nonce = os.urandom(_NONCE_SIZE)

        # Encrypt with AES-256-GCM
        key = _derive_key(secret)
        cipher = AESGCM(key)
        ciphertext_with_tag = cipher.encrypt(nonce, plaintext, None)

        # Return nonce || ciphertext || tag
        return nonce + ciphertext_with_tag
    except Exception as e:
        raise KVEncryptionError(f"Failed to encrypt value: {e}")


def decrypt_value(secret: str, encrypted: bytes) -> Any:
    """Decrypt an AES-256-GCM encrypted value.

    Args:
        secret: The encryption secret (AUTOMATION_KV_SECRET)
        encrypted: Encrypted bytes (nonce + ciphertext + tag)

    Returns:
        The decrypted JSON value

    Raises:
        KVEncryptionError: If decryption fails (wrong key, tampered data, etc.)
    """
    try:
        if len(encrypted) < _NONCE_SIZE + _TAG_SIZE:
            raise KVEncryptionError("Encrypted data too short")

        # Split nonce from ciphertext+tag
        nonce = encrypted[:_NONCE_SIZE]
        ciphertext_with_tag = encrypted[_NONCE_SIZE:]

        # Decrypt with AES-256-GCM
        key = _derive_key(secret)
        cipher = AESGCM(key)
        plaintext = cipher.decrypt(nonce, ciphertext_with_tag, None)

        # Parse JSON
        return json.loads(plaintext.decode("utf-8"))
    except KVEncryptionError:
        raise
    except Exception as e:
        raise KVEncryptionError(f"Failed to decrypt value: {e}")
