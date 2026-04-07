"""Tests for webhook utility functions."""

import hashlib
import hmac

from automation.utils.webhook import verify_signature


class TestVerifySignature:
    """Tests for HMAC signature verification."""

    def test_valid_signature(self):
        """Valid signature should return True."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        # Generate valid signature
        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        assert verify_signature(payload, signature, secret) is True

    def test_invalid_signature(self):
        """Invalid signature should return False."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        # Wrong signature (64 hex chars)
        signature = "sha256=" + "0" * 64

        assert verify_signature(payload, signature, secret) is False

    def test_signature_formats(self):
        """Both raw hex and sha256= prefixed signatures should work."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        # Generate valid hash
        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        # Raw hex without prefix should work (e.g., Linear's format)
        assert verify_signature(payload, expected_sig, secret) is True

        # With sha256= prefix should also work (GitHub's format)
        assert verify_signature(payload, f"sha256={expected_sig}", secret) is True

        # Wrong prefix should fail (sha1= is not supported)
        assert verify_signature(payload, f"sha1={expected_sig}", secret) is False

    def test_empty_signature(self):
        """Empty signature should return False."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        assert verify_signature(payload, "", secret) is False

    def test_different_secret(self):
        """Signature with different secret should return False."""
        payload = b'{"event": "test"}'
        secret1 = "secret-one"
        secret2 = "secret-two"

        # Sign with secret1
        expected_sig = hmac.new(secret1.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        # Verify with secret2 - should fail
        assert verify_signature(payload, signature, secret2) is False

    def test_modified_payload(self):
        """Signature should fail if payload is modified."""
        original_payload = b'{"event": "test"}'
        modified_payload = b'{"event": "modified"}'
        secret = "test-secret-key"

        # Sign original
        expected_sig = hmac.new(
            secret.encode(), original_payload, hashlib.sha256
        ).hexdigest()
        signature = f"sha256={expected_sig}"

        # Verify modified - should fail
        assert verify_signature(modified_payload, signature, secret) is False

    def test_unicode_payload(self):
        """Signature verification should work with unicode payloads."""
        payload = '{"message": "こんにちは"}'.encode()
        secret = "test-secret-key"

        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        assert verify_signature(payload, signature, secret) is True

    def test_empty_payload(self):
        """Empty payload should still verify correctly."""
        payload = b""
        secret = "test-secret-key"

        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        assert verify_signature(payload, signature, secret) is True
