"""Tests for application-layer encryption.

Covers EncryptedString and EncryptedJSONHeaders TypeDecorators and the
get_cipher() helper.  Uses the SDK Cipher (openhands.sdk.utils.cipher).
Pure unit tests — no database or Docker required.
"""

import os
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from openhands.automation.utils.encrypted_fields import (
    EncryptedJSONHeaders,
    EncryptedString,
    _is_secret_header,
    get_cipher,
)
from openhands.sdk.utils.cipher import FERNET_TOKEN_PREFIX, Cipher


TEST_KEY = "test-secret-key-for-automation-service"


def _encrypt(cipher: Cipher, plaintext: str) -> str:
    """Thin wrapper: SDK Cipher.encrypt takes SecretStr; returns str."""
    result = cipher.encrypt(SecretStr(plaintext))
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# get_cipher helper
# ---------------------------------------------------------------------------


class TestGetCipher:
    def test_returns_cipher_when_automation_key_set(self):
        with patch.dict(os.environ, {"AUTOMATION_SECRET_KEY": TEST_KEY}, clear=False):
            assert isinstance(get_cipher(), Cipher)

    def test_falls_back_to_oh_secret_key(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("AUTOMATION_SECRET_KEY", "OH_SECRET_KEY")
        }
        env["OH_SECRET_KEY"] = TEST_KEY
        with patch.dict(os.environ, env, clear=True):
            assert get_cipher() is not None

    def test_returns_none_when_no_key_set(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("AUTOMATION_SECRET_KEY", "OH_SECRET_KEY")
        }
        with patch.dict(os.environ, env, clear=True):
            assert get_cipher() is None

    def test_automation_key_takes_precedence_over_oh_key(self):
        env = dict(os.environ)
        env["AUTOMATION_SECRET_KEY"] = "automation-key"
        env["OH_SECRET_KEY"] = "oh-key"
        with patch.dict(os.environ, env, clear=True):
            cipher = get_cipher()
            assert cipher is not None
            ct = _encrypt(cipher, "value")
            assert Cipher("automation-key").try_decrypt_str(ct) == "value"


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
            result = col.process_bind_param("xapp-token", None)
            assert result is not None
            assert result.startswith(FERNET_TOKEN_PREFIX)

    def test_decrypt_on_result_value(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        ciphertext = _encrypt(cipher, "xapp-token")
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            result = col.process_result_value(ciphertext, None)
            assert result == "xapp-token"

    def test_none_passthrough(self):
        col = self._make_col()
        assert col.process_bind_param(None, None) is None
        assert col.process_result_value(None, None) is None

    def test_no_cipher_stores_plaintext(self):
        col = self._make_col()
        with patch(
            "openhands.automation.utils.encrypted_fields.get_cipher", return_value=None
        ):
            result = col.process_bind_param("xapp-token", None)
            assert result == "xapp-token"

    def test_no_cipher_reads_plaintext(self):
        col = self._make_col()
        with patch(
            "openhands.automation.utils.encrypted_fields.get_cipher", return_value=None
        ):
            result = col.process_result_value("xapp-token", None)
            assert result == "xapp-token"

    def test_read_plaintext_row_with_cipher_present(self):
        """Plaintext rows written before key was set should still be readable."""
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            # Value does not have Fernet prefix → returned as-is
            result = col.process_result_value("xapp-1-old-plaintext-token", None)
            assert result == "xapp-1-old-plaintext-token"

    def test_roundtrip_bind_then_result(self):
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            encrypted = col.process_bind_param("my-secret", None)
            decrypted = col.process_result_value(encrypted, None)
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
            stored = col.process_bind_param(headers, None)

        assert stored is not None
        assert stored["Authorization"].startswith(FERNET_TOKEN_PREFIX)
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
            stored = col.process_bind_param(headers, None)
            assert stored is not None
            restored = col.process_result_value(stored, None)

        assert restored is not None
        assert restored["Authorization"] == "Bearer secret-token"
        assert restored["Content-Type"] == "application/json"

    def test_none_passthrough(self):
        col = self._make_col()
        assert col.process_bind_param(None, None) is None
        assert col.process_result_value(None, None) is None

    def test_empty_dict_passthrough(self):
        col = self._make_col()
        assert col.process_bind_param({}, None) == {}

    def test_no_cipher_stores_plaintext(self):
        col = self._make_col()
        headers = {"Authorization": "Bearer token", "X-Api-Key": "key123"}
        with patch(
            "openhands.automation.utils.encrypted_fields.get_cipher", return_value=None
        ):
            result = col.process_bind_param(headers, None)
        assert result == headers

    def test_plaintext_headers_readable_after_key_introduced(self):
        """Rows stored before key was set should read back cleanly."""
        col = self._make_col()
        cipher = Cipher(TEST_KEY)
        plaintext_stored = {"Authorization": "Bearer old-token"}
        with patch("openhands.automation.utils.encrypted_fields.get_cipher") as mock:
            mock.return_value = cipher
            result = col.process_result_value(plaintext_stored, None)
        # Value doesn't have Fernet prefix → returned as-is
        assert result is not None
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
            stored = col.process_bind_param(headers, None)

        assert stored is not None
        assert stored["Authorization"].startswith(FERNET_TOKEN_PREFIX)
        assert stored["X-Api-Key"].startswith(FERNET_TOKEN_PREFIX)
        assert stored["Cookie"].startswith(FERNET_TOKEN_PREFIX)
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
