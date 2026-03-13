"""Encryption utilities for storing user API keys.

Uses Fernet symmetric encryption. In production,
AUTOMATION_ENCRYPTION_KEY should be a securely generated
base64-encoded 32-byte key. Generate one with:

    python -c "from cryptography.fernet import Fernet; \\
        print(Fernet.generate_key().decode())"
"""

import logging

from cryptography.fernet import Fernet, InvalidToken

from automation.config import get_settings


logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = get_settings().encryption_key
        if not key:
            raise ValueError("AUTOMATION_ENCRYPTION_KEY must be set.")
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_api_key(api_key: str) -> str:
    return _get_fernet().encrypt(api_key.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt API key — invalid token or wrong key")
        raise ValueError("Cannot decrypt API key")
