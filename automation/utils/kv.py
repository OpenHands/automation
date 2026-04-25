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


def create_kv_token(
    secret: str,
    automation_id: uuid.UUID,
    run_id: uuid.UUID,
) -> str:
    """Create a JWT token for KV store access.

    The token embeds the automation_id as a trusted claim, ensuring
    that KV operations are scoped to the correct automation.

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


def verify_kv_token(secret: str, token: str) -> uuid.UUID:
    """Verify a KV store JWT token and extract the automation_id.

    Args:
        secret: The signing secret (AUTOMATION_KV_SECRET)
        token: The JWT token to verify

    Returns:
        The automation_id UUID from the verified token

    Raises:
        KVTokenError: If token is invalid, expired, or malformed
    """
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        automation_id_str = payload.get("automation_id")
        if not automation_id_str:
            raise KVTokenError("Token missing automation_id claim")
        return uuid.UUID(automation_id_str)
    except jwt.ExpiredSignatureError:
        raise KVTokenError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise KVTokenError(f"Invalid token: {e}")
    except ValueError as e:
        raise KVTokenError(f"Invalid automation_id format: {e}")


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

    The value is JSON-serialized, then encrypted. The result is raw bytes
    suitable for storage in a BYTEA column.

    Wire format: nonce (12 bytes) || ciphertext || auth_tag (16 bytes)

    Args:
        secret: The encryption secret (AUTOMATION_KV_SECRET)
        value: Any JSON-serializable value

    Returns:
        Encrypted bytes (nonce + ciphertext + tag)

    Raises:
        KVEncryptionError: If encryption fails
    """
    try:
        # Serialize value to JSON bytes
        plaintext = json.dumps(value).encode("utf-8")

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
