"""Tests for encryption utilities."""

import pytest
from cryptography.fernet import Fernet

from automation.encryption import decrypt_api_key, encrypt_api_key


def test_encrypt_decrypt_roundtrip(settings):
    """Encrypting then decrypting should return the original value."""
    original = "sk-oh-mytestkey12345"
    encrypted = encrypt_api_key(original)
    assert encrypted != original
    decrypted = decrypt_api_key(encrypted)
    assert decrypted == original


def test_decrypt_wrong_key(settings):
    """Decrypting with a different key should raise ValueError."""
    original = "sk-oh-somekey"
    encrypted = encrypt_api_key(original)

    # Swap the fernet instance to a different key
    from automation import encryption as enc_module

    enc_module._fernet = Fernet(Fernet.generate_key())

    with pytest.raises(ValueError, match="Cannot decrypt"):
        decrypt_api_key(encrypted)


def test_encrypt_produces_different_ciphertexts(settings):
    """Two encryptions of the same value should differ (Fernet uses random IV)."""
    original = "sk-oh-samekey"
    a = encrypt_api_key(original)
    b = encrypt_api_key(original)
    assert a != b  # Different IVs
    assert decrypt_api_key(a) == decrypt_api_key(b) == original
