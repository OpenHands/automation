"""Tests for application-layer encryption: Cipher, EncryptedString, EncryptedJSONHeaders.

All tests in this module are pure unit tests — no database or Docker required.
"""

import os
from unittest.mock import patch

import pytest

from openhands.automation.utils.cipher import (
    FERNET_TOKEN_PREFIX,
    Cipher,
    get_cipher,
)
from openhands.automation.utils.encrypted_fields import (
    EncryptedJSONHeaders,
    EncryptedString,
    _is_secret_header,
)


TEST_KEY = "test-secret-key-for-automation-service"


# ---------------------------------------------------------------------------
# Cipher
# ---------------------------------------------------------------------------


class TestCipher:
    def test_encrypt_decrypt_roundtrip(self):
        cipher = Cipher(TEST_KEY)
        plaintext = "xapp-1-AAAAAAAAA-1111111111-aaaaaaaaaaaaaaaa"
        ciphertext = cipher.encrypt(plaintext)
        assert cipher.decrypt(ciphertext) == plaintext

    def test_ciphertext_is_string(self):
        cipher = Cipher(TEST_KEY)
        ct = cipher.encrypt("hello")
        assert isinstance(ct, str)

    def test_ciphertext_has_fernet_prefix(self):
        cipher = Cipher(TEST_KEY)
        ct = cipher.encrypt("hello")
        assert cipher.is_ciphertext(ct)
        assert ct.startswith(FERNET_TOKEN_PREFIX)

    def test_plaintext_is_not_ciphertext(self):
        cipher = Cipher(TEST_KEY)
        assert not cipher.is_ciphertext("xapp-1-AAAAAAAAA")
        assert not cipher.is_ciphertext("Bearer token123")
        assert not cipher.is_ciphertext("")

    def test_decrypt_invalid_returns_none(self):
        cipher = Cipher(TEST_KEY)
        result = cipher.decrypt("not-a-valid-fernet-token")
        assert result is None

    def test_decrypt_wrong_key_returns_none(self):
        cipher1 = Cipher("key-one")
        cipher2 = Cipher("key-two")
        ct = cipher1.encrypt("secret")
        assert cipher2.decrypt(ct) is None

    def test_different_plaintexts_produce_different_ciphertexts(self):
        cipher = Cipher(TEST_KEY)
        ct1 = cipher.encrypt("secret-a")
        ct2 = cipher.encrypt("secret-b")
        assert ct1 != ct2

    def test_same_plaintext_produces_different_ciphertexts(self):
        # Fernet uses random nonces — two encryptions of the same plaintext differ
        cipher = Cipher(TEST_KEY)
        ct1 = cipher.encrypt("same")
        ct2 = cipher.encrypt("same")
        assert ct1 != ct2
        assert cipher.decrypt(ct1) == cipher.decrypt(ct2) == "same"


class TestGetCipher:
    def test_returns_cipher_when_automation_key_set(self):
        with patch.dict(os.environ, {"AUTOMATION_SECRET_KEY": TEST_KEY}, clear=False):
            cipher = get_cipher()
            assert cipher is not None
            assert isinstance(cipher, Cipher)

    def test_falls_back_to_oh_secret_key(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AUTOMATION_SECRET_KEY", "OH_SECRET_KEY")}
        env["OH_SECRET_KEY"] = TEST_KEY
        with patch.dict(os.environ, env, clear=True):
            cipher = get_cipher()
            assert cipher is not None

    def test_returns_none_when_no_key_set(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AUTOMATION_SECRET_KEY", "OH_SECRET_KEY")}
        with patch.dict(os.environ, env, clear=True):
            cipher = get_cipher()
            assert cipher is None

    def test_automation_key_takes_precedence_over_oh_key(self):
        env = {k: v for k, v in os.environ.items()}
        env["AUTOMATION_SECRET_KEY"] = "automation-key"
        env["OH_SECRET_KEY"] = "oh-key"
        with patch.dict(os.environ, env, clear=True):
            cipher = get_cipher()
            assert cipher is not None
            # automation key should be used
            ct = cipher.encrypt("value")
            assert Cipher("automation-key").decrypt(ct) == "value"


# ---------------------------------------------------------------------------
# _is_secret_header
# ---------------------------------------------------------------------------


class TestIsSecretHeader:
    @pytest.mark.parametrize(
        "header",
        [
            "Authorization",
            "AUTHORIZATION",
            "authorization",
            "X-Api-Key",
            "x-api-key",
            "X-SECRET-HEADER",
            "Cookie",
            "cookie",
            "password",
            "X-Auth-Token",
            "session-token",
        ],
    )
    def test_secret_headers_detected(self, header):
        assert _is_secret_header(header)

    @pytest.mark.parametrize(
        "header",
        [
            "Content-Type",
            "Accept",
            "User-Agent",
            "X-Request-ID",
            "X-Correlation-ID",
            "Cache-Control",
        ],
    )
    def test_non_secret_headers_not_detected(self, header):
        assert not _is_secret_header(header)


# ---------------------------------------------------------------------------
# EncryptedString TypeDecorator
# ---------------------------------------------------------------------------


class TestEncryptedString:
    def _make_col(self):
        return EncryptedString(255)

    def test_encrypt_on_bind_param(self):
        col = self._make_col()
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            cipher = Cipher(TEST_KEY)
            mock.return_value = cipher
            result = col.process_bind_param("xapp-token", dialect=None)
            assert result is not None
            assert cipher.is_ciphertext(result)

    def test_decrypt_on_result_value(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        ciphertext = cipher.encrypt("xapp-token")
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            result = col.process_result_value(ciphertext, dialect=None)
            assert result == "xapp-token"

    def test_none_passthrough(self):
        col = self._make_col()
        assert col.process_bind_param(None, dialect=None) is None
        assert col.process_result_value(None, dialect=None) is None

    def test_no_cipher_stores_plaintext(self):
        col = self._make_col()
        with patch("openhands.automation.utils.encrypted_fields.get_cipher", return_value=None):
            result = col.process_bind_param("xapp-token", dialect=None)
            assert result == "xapp-token"

    def test_no_cipher_reads_plaintext(self):
        col = self._make_col()
        with patch("openhands.automation.utils.encrypted_fields.get_cipher", return_value=None):
            result = col.process_result_value("xapp-token", dialect=None)
            assert result == "xapp-token"

    def test_read_plaintext_row_with_cipher_present(self):
        """Plaintext rows written before key was set should still be readable."""
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            # Value does not have Fernet prefix → returned as-is
            result = col.process_result_value("xapp-1-old-plaintext-token", dialect=None)
            assert result == "xapp-1-old-plaintext-token"

    def test_roundtrip_bind_then_result(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            encrypted = col.process_bind_param("my-secret", dialect=None)
            decrypted = col.process_result_value(encrypted, dialect=None)
            assert decrypted == "my-secret"


# ---------------------------------------------------------------------------
# EncryptedJSONHeaders TypeDecorator
# ---------------------------------------------------------------------------


class TestEncryptedJSONHeaders:
    def _make_col(self):
        return EncryptedJSONHeaders()

    def test_encrypts_auth_header_only(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        headers = {
            "Authorization": "Bearer secret-token",
            "Content-Type": "application/json",
            "X-Request-ID": "req-123",
        }
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            stored = col.process_bind_param(headers, dialect=None)

        assert cipher.is_ciphertext(stored["Authorization"])
        assert stored["Content-Type"] == "application/json"
        assert stored["X-Request-ID"] == "req-123"

    def test_decrypts_auth_header_only(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        headers = {
            "Authorization": "Bearer secret-token",
            "Content-Type": "application/json",
        }
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            stored = col.process_bind_param(headers, dialect=None)
            restored = col.process_result_value(stored, dialect=None)

        assert restored["Authorization"] == "Bearer secret-token"
        assert restored["Content-Type"] == "application/json"

    def test_none_passthrough(self):
        col = self._make_col()
        assert col.process_bind_param(None, dialect=None) is None
        assert col.process_result_value(None, dialect=None) is None

    def test_empty_dict_passthrough(self):
        col = self._make_col()
        assert col.process_bind_param({}, dialect=None) == {}

    def test_no_cipher_stores_plaintext(self):
        col = self._make_col()
        headers = {"Authorization": "Bearer token", "X-Api-Key": "key123"}
        with patch("openhands.automation.utils.encrypted_fields.get_cipher", return_value=None):
            result = col.process_bind_param(headers, dialect=None)
        assert result == headers

    def test_plaintext_headers_readable_after_key_introduced(self):
        """Rows stored before key was set should read back cleanly."""
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        plaintext_stored = {"Authorization": "Bearer old-token"}
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            result = col.process_result_value(plaintext_stored, dialect=None)
        # Value doesn't have Fernet prefix → returned as-is
        assert result["Authorization"] == "Bearer old-token"

    def test_multiple_secret_headers_encrypted(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        headers = {
            "Authorization": "Bearer tok",
            "X-Api-Key": "apikey123",
            "Cookie": "session=abc",
            "Accept": "application/json",
        }
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            stored = col.process_bind_param(headers, dialect=None)

        assert cipher.is_ciphertext(stored["Authorization"])
        assert cipher.is_ciphertext(stored["X-Api-Key"])
        assert cipher.is_ciphertext(stored["Cookie"])
        assert stored["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# Update endpoint kind-specific validation
# ---------------------------------------------------------------------------


class TestUpdateEndpointKindValidation:
    """Schema-level tests for the update endpoint's kind constraint."""

    def test_url_field_present_in_update_schema(self):
        from openhands.automation.schemas import WebSocketSourceUpdate

        u = WebSocketSourceUpdate(url="wss://new.example.com")
        assert u.url == "wss://new.example.com"

    def test_app_token_field_present_in_update_schema(self):
        from openhands.automation.schemas import WebSocketSourceUpdate

        u = WebSocketSourceUpdate(app_token="xapp-1-NEW")
        assert u.app_token == "xapp-1-NEW"

    def test_url_can_be_set_to_none_in_schema(self):
        """Schema accepts None (validation is enforced at the router layer)."""
        from openhands.automation.schemas import WebSocketSourceUpdate

        u = WebSocketSourceUpdate(url=None)
        assert u.url is None
