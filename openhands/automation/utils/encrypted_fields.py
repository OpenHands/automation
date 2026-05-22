"""SQLAlchemy TypeDecorators for application-layer encryption.

Provides two column types that transparently encrypt/decrypt values using the
``Cipher`` from ``openhands.automation.utils.cipher``:

``EncryptedString``
    For single string columns (e.g. ``app_token``).  Stores Fernet ciphertext
    when a key is configured; falls back to plaintext otherwise.

``EncryptedJSONHeaders``
    For ``dict[str, str]`` header columns.  Only encrypts values whose *key*
    matches sensitive-header patterns (``Authorization``, ``Cookie``,
    ``X-Api-Key``, etc.) to match the behaviour of ``LookupSecret._serialize_secrets``
    in the OpenHands SDK.  Non-sensitive headers are stored unencrypted.

Migration safety
----------------
Both decoders check ``Cipher.is_ciphertext`` before attempting decryption, so
existing plaintext rows continue to work after the key is introduced — no data
migration is required.  Once a row is updated its value will be re-encrypted.

If ``AUTOMATION_SECRET_KEY`` / ``OH_SECRET_KEY`` is absent a one-time WARNING
is emitted and values are stored as plaintext (preserving current behaviour).
"""

import logging

from sqlalchemy import String
from sqlalchemy.types import JSON, TypeDecorator

from openhands.automation.utils.cipher import Cipher, get_cipher


logger = logging.getLogger("automation.utils.encrypted_fields")

# Header-key patterns whose *values* are considered sensitive.
# Mirrors ``SECRET_KEY_PATTERNS`` from ``openhands.sdk.utils.redact``.
_SECRET_HEADER_PATTERNS = frozenset(
    {
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "KEY",
        "PASSWORD",
        "SECRET",
        "SESSION",
        "TOKEN",
    }
)

_warned_no_cipher = False  # emit the "no key configured" warning only once


def _warn_no_cipher(field: str) -> None:
    global _warned_no_cipher
    if not _warned_no_cipher:
        logger.warning(
            "AUTOMATION_SECRET_KEY / OH_SECRET_KEY is not set — sensitive "
            "field '%s' (and others) will be stored as plaintext. "
            "Set the key to enable encryption at rest.",
            field,
        )
        _warned_no_cipher = True


def _is_secret_header(key: str) -> bool:
    """Return True if the header key name indicates a sensitive value."""
    upper = key.upper()
    return any(pattern in upper for pattern in _SECRET_HEADER_PATTERNS)


class EncryptedString(TypeDecorator):
    """A ``String`` column that is transparently encrypted/decrypted.

    Matches the per-field encryption pattern used by ``StaticSecret.value``
    in the OpenHands SDK: the Fernet cipher is applied on the way in and out
    of the database; the ORM always works with plaintext strings.

    If no cipher key is configured the column behaves as a plain ``String``.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, _dialect) -> str | None:
        """Encrypt on the way TO the database."""
        if value is None:
            return None
        cipher: Cipher | None = get_cipher()
        if cipher is None:
            _warn_no_cipher(self.__class__.__name__)
            return value
        return cipher.encrypt(value)

    def process_result_value(self, value: str | None, _dialect) -> str | None:
        """Decrypt on the way FROM the database."""
        if value is None:
            return None
        cipher: Cipher | None = get_cipher()
        if cipher is None:
            return value  # stored as plaintext (no key at write time)
        if cipher.is_ciphertext(value):
            return cipher.decrypt(value)
        return value  # plaintext row written before key was introduced


class EncryptedJSONHeaders(TypeDecorator):
    """A ``JSON`` column storing ``dict[str, str]`` headers.

    Only the values of *sensitive* header keys (matching
    ``_SECRET_HEADER_PATTERNS``) are encrypted, mirroring the behaviour of
    ``LookupSecret._serialize_secrets`` / ``_validate_secrets`` in the
    OpenHands SDK.  Non-sensitive headers (e.g. ``Content-Type``) are stored
    as-is to keep the stored document human-readable in non-critical cases.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: dict | None, _dialect) -> dict | None:
        """Encrypt sensitive header values on the way TO the database."""
        if not value:
            return value
        cipher: Cipher | None = get_cipher()
        if cipher is None:
            _warn_no_cipher("headers")
            return value
        result: dict = {}
        for k, v in value.items():
            if _is_secret_header(k) and isinstance(v, str) and v:
                result[k] = cipher.encrypt(v)
            else:
                result[k] = v
        return result

    def process_result_value(self, value: dict | None, _dialect) -> dict | None:
        """Decrypt sensitive header values on the way FROM the database."""
        if not value:
            return value
        cipher: Cipher | None = get_cipher()
        if cipher is None:
            return value
        result: dict = {}
        for k, v in value.items():
            if _is_secret_header(k) and isinstance(v, str) and cipher.is_ciphertext(v):
                decrypted = cipher.decrypt(v)
                result[k] = decrypted if decrypted is not None else v
            else:
                result[k] = v
        return result
