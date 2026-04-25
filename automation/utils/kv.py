"""KV store utilities: JWT tokens and JWE encryption.

This module provides:
- JWT token generation/verification for KV store authentication
- JWE encryption/decryption for KV values

All KV values are encrypted at the application level before storage.
JWT tokens are scoped per-automation run with short expiration.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwcrypto import jwe, jwk


class KVTokenError(Exception):
    """Error with KV store JWT token."""

    pass


class KVEncryptionError(Exception):
    """Error with KV value encryption/decryption."""

    pass


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


def _get_jwe_key(secret: str) -> jwk.JWK:
    """Derive a JWK symmetric key from the secret.

    Uses the first 32 bytes of the secret (or pads if shorter)
    as a 256-bit symmetric key for AES-256-GCM encryption.
    """
    # Ensure we have exactly 32 bytes for AES-256
    key_bytes = secret.encode("utf-8")[:32].ljust(32, b"\0")
    return jwk.JWK(kty="oct", k=jwk.base64url_encode(key_bytes))  # type: ignore[attr-defined]


def encrypt_value(secret: str, value: Any) -> str:
    """Encrypt a value for storage using JWE.

    The value is JSON-serialized, then encrypted with AES-256-GCM.

    Args:
        secret: The encryption secret (AUTOMATION_KV_SECRET)
        value: Any JSON-serializable value

    Returns:
        JWE compact serialization string

    Raises:
        KVEncryptionError: If encryption fails
    """
    try:
        # Serialize value to JSON
        plaintext = json.dumps(value)

        # Create JWE token
        key = _get_jwe_key(secret)
        token = jwe.JWE(
            plaintext.encode("utf-8"),
            recipient=key,  # type: ignore[arg-type]
            protected={  # type: ignore[arg-type]
                "alg": "dir",  # Direct encryption (no key wrapping)
                "enc": "A256GCM",  # AES-256-GCM
            },
        )
        return token.serialize(compact=True)
    except Exception as e:
        raise KVEncryptionError(f"Failed to encrypt value: {e}")


def decrypt_value(secret: str, encrypted: str) -> Any:
    """Decrypt a JWE-encrypted value.

    Args:
        secret: The encryption secret (AUTOMATION_KV_SECRET)
        encrypted: JWE compact serialization string

    Returns:
        The decrypted JSON value

    Raises:
        KVEncryptionError: If decryption fails
    """
    try:
        key = _get_jwe_key(secret)
        token = jwe.JWE()
        token.deserialize(encrypted, key)
        plaintext = token.payload.decode("utf-8")
        return json.loads(plaintext)
    except Exception as e:
        raise KVEncryptionError(f"Failed to decrypt value: {e}")
