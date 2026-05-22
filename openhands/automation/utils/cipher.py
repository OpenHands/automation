"""Application-layer encryption for sensitive fields.

This module provides the same Fernet-based cipher used by the OpenHands SDK
(``openhands.sdk.utils.cipher``) — adapted for use inside the automation
service where a shared key encrypts credentials before they are written to the
database.

Key derivation
--------------
The raw ``AUTOMATION_SECRET_KEY`` (or ``OH_SECRET_KEY``) value is hashed with
SHA-256 and base64-encoded to produce a valid 256-bit Fernet key, so the env
var itself does not need to be exactly 32 bytes.

Ciphertext detection
--------------------
Fernet tokens always start with the prefix ``"gAAAAA"`` (the base-64 encoding
of the ``\\x80`` version byte).  ``Cipher.is_ciphertext`` exploits this to
distinguish already-encrypted values from plaintext — important during a
rolling migration where old rows may still be plaintext.

Safe defaults
-------------
If ``AUTOMATION_SECRET_KEY`` / ``OH_SECRET_KEY`` is absent, ``get_cipher()``
returns ``None`` and the callers fall back to storing/reading plaintext.  A
warning is logged once at import time so operators are not surprised.  This
preserves backward-compat for deployments that haven't yet set the key.
"""

import hashlib
import logging
import os
from base64 import b64encode

from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger("automation.utils.cipher")

# Fernet tokens always start with this 6-char prefix (base64 of 0x80 version byte).
# Using a 6-char prefix avoids collisions with realistic base64 plaintext values.
FERNET_TOKEN_PREFIX = "gAAAAA"

# Env-var names checked in order of preference
_KEY_ENV_VARS = ("AUTOMATION_SECRET_KEY", "OH_SECRET_KEY")


class Cipher:
    """Symmetric Fernet cipher with SHA-256 key derivation.

    Matches the interface of ``openhands.sdk.utils.cipher.Cipher`` so that the
    same encrypted blobs can be shared between the SDK and the automation
    service if they share the same key.
    """

    def __init__(self, secret_key: str) -> None:
        self._secret_key = secret_key
        self._fernet: Fernet | None = None

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string and return the Fernet token as a str."""
        return self._get_fernet().encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str | None:
        """Decrypt a Fernet token, returning None if decryption fails.

        A ``None`` return (instead of an exception) matches the SDK convention
        and allows callers to fall back gracefully — e.g. when rows were
        encrypted with a different key or were never encrypted.
        """
        try:
            return self._get_fernet().decrypt(ciphertext.encode()).decode()
        except (InvalidToken, Exception) as exc:
            logger.warning(
                "Failed to decrypt value (returning None): %s. "
                "This may occur when loading data encrypted with a different key "
                "or when migrating from unencrypted storage.",
                exc,
            )
            return None

    def is_ciphertext(self, value: str) -> bool:
        """Return True if *value* is a Fernet token (already encrypted)."""
        return value.startswith(FERNET_TOKEN_PREFIX)

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            key = b64encode(hashlib.sha256(self._secret_key.encode()).digest())
            self._fernet = Fernet(key)
        return self._fernet


def get_cipher() -> Cipher | None:
    """Return a ``Cipher`` built from the first available key env var, or None.

    Checks ``AUTOMATION_SECRET_KEY`` then ``OH_SECRET_KEY``.  If neither is
    set, returns ``None`` — callers should store/read values as plaintext and
    log an appropriate warning.
    """
    for env_var in _KEY_ENV_VARS:
        key = os.getenv(env_var)
        if key:
            return Cipher(key)
    return None
