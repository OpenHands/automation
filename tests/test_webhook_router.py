"""Tests for custom webhook CRUD endpoints."""

import uuid

import pytest
from pydantic import ValidationError

from openhands.automation.schemas import (
    RESERVED_SOURCES,
    CustomWebhookCreate,
    CustomWebhookUpdate,
)


class TestCustomWebhookCreateSchema:
    """Tests for CustomWebhookCreate validation."""

    def test_valid_create(self):
        """Valid webhook creation data."""
        data = CustomWebhookCreate(
            name="Stripe Payments",
            source="stripe",
            event_key_expr="type",
        )
        assert data.name == "Stripe Payments"
        assert data.source == "stripe"
        assert data.event_key_expr == "type"
        assert data.signature_header == "X-Signature-256"  # default
        assert data.webhook_secret is None  # optional

    def test_source_normalized_to_lowercase(self):
        """Source should be normalized to lowercase."""
        data = CustomWebhookCreate(name="Test", source="MySource")
        assert data.source == "mysource"

    def test_source_with_hyphens(self):
        """Source can contain hyphens."""
        data = CustomWebhookCreate(name="Test", source="my-custom-source")
        assert data.source == "my-custom-source"

    def test_source_with_numbers(self):
        """Source can contain numbers."""
        data = CustomWebhookCreate(name="Test", source="service123")
        assert data.source == "service123"

    def test_single_char_source(self):
        """Single character source is valid."""
        data = CustomWebhookCreate(name="Test", source="s")
        assert data.source == "s"

    def test_reserved_source_github_rejected(self):
        """Reserved source 'github' should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="github")
        assert "reserved source name" in str(exc_info.value)

    def test_reserved_source_jira_dc_rejected(self):
        """Reserved source 'jira_dc' should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="jira_dc")
        assert "reserved source name" in str(exc_info.value)

    def test_reserved_source_bitbucket_data_center_rejected(self):
        """Reserved source 'bitbucket_data_center' should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="bitbucket_data_center")
        assert "reserved source name" in str(exc_info.value)

    def test_reserved_source_case_insensitive(self):
        """Reserved source check is case-insensitive."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="GitHub")
        assert "reserved source name" in str(exc_info.value)

    def test_source_cannot_start_with_hyphen(self):
        """Source cannot start with hyphen."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="-invalid")
        assert "alphanumeric" in str(exc_info.value).lower()

    def test_source_cannot_end_with_hyphen(self):
        """Source cannot end with hyphen."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="invalid-")
        assert "alphanumeric" in str(exc_info.value).lower()

    def test_source_no_special_chars(self):
        """Source cannot contain special characters."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="my_source")
        assert "alphanumeric" in str(exc_info.value).lower()

    def test_source_too_long(self):
        """Source longer than 50 chars is rejected."""
        with pytest.raises(ValidationError):
            CustomWebhookCreate(name="Test", source="a" * 51)

    def test_invalid_event_key_expr(self):
        """Invalid JMESPath expression is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(name="Test", source="test", event_key_expr="[invalid")
        assert "JMESPath" in str(exc_info.value)

    def test_valid_complex_event_key_expr(self):
        """Complex JMESPath expressions are valid."""
        data = CustomWebhookCreate(
            name="Test",
            source="test",
            event_key_expr="event.type || metadata.action",
        )
        assert data.event_key_expr == "event.type || metadata.action"

    def test_default_event_key_expr(self):
        """Default event_key_expr is 'type'."""
        data = CustomWebhookCreate(name="Test", source="test")
        assert data.event_key_expr == "type"

    def test_custom_signature_header(self):
        """Custom signature header can be specified."""
        data = CustomWebhookCreate(
            name="Stripe",
            source="stripe",
            signature_header="Stripe-Signature",
        )
        assert data.signature_header == "Stripe-Signature"

    def test_signature_header_validation(self):
        """Signature header must be valid HTTP header name."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(
                name="Test",
                source="test",
                signature_header="Invalid Header!",
            )
        assert "alphanumeric" in str(exc_info.value).lower()

    def test_signature_header_cannot_start_with_number(self):
        """Signature header must start with a letter."""
        with pytest.raises(ValidationError):
            CustomWebhookCreate(
                name="Test",
                source="test",
                signature_header="123-Header",
            )

    def test_user_provided_secret(self):
        """User can provide their own webhook secret."""
        data = CustomWebhookCreate(
            name="External Service",
            source="external",
            webhook_secret="my-external-service-secret-key",
        )
        assert data.webhook_secret == "my-external-service-secret-key"

    def test_user_provided_secret_min_length(self):
        """User-provided secret must be at least 8 characters."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookCreate(
                name="Test",
                source="test",
                webhook_secret="short",
            )
        assert "8" in str(exc_info.value)


class TestCustomWebhookUpdateSchema:
    """Tests for CustomWebhookUpdate validation."""

    def test_all_fields_optional(self):
        """All fields are optional for update."""
        data = CustomWebhookUpdate()
        assert data.name is None
        assert data.event_key_expr is None
        assert data.signature_header is None
        assert data.enabled is None

    def test_partial_update(self):
        """Partial updates work."""
        data = CustomWebhookUpdate(name="New Name")
        assert data.name == "New Name"
        assert data.event_key_expr is None

    def test_disable_webhook(self):
        """Can disable a webhook."""
        data = CustomWebhookUpdate(enabled=False)
        assert data.enabled is False

    def test_invalid_event_key_expr_update(self):
        """Invalid JMESPath in update is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookUpdate(event_key_expr="[invalid")
        assert "JMESPath" in str(exc_info.value)

    def test_update_signature_header(self):
        """Can update signature header."""
        data = CustomWebhookUpdate(signature_header="X-Custom-Sig")
        assert data.signature_header == "X-Custom-Sig"

    def test_invalid_signature_header_update(self):
        """Invalid signature header in update is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            CustomWebhookUpdate(signature_header="Invalid Header!")
        assert "alphanumeric" in str(exc_info.value).lower()


class TestReservedSources:
    """Tests for reserved source names."""

    def test_github_is_reserved(self):
        """GitHub should be reserved."""
        assert "github" in RESERVED_SOURCES

    def test_builtin_sources_reserved(self):
        """Built-in sources should be reserved."""
        assert RESERVED_SOURCES == {"bitbucket_data_center", "github", "jira_dc"}


class TestWebhookSecretGeneration:
    """Tests for webhook secret generation."""

    def test_secret_format(self):
        """Generated secrets should have correct format."""
        from openhands.automation.webhook_router import _generate_webhook_secret

        secret = _generate_webhook_secret()
        assert secret.startswith("whsec_")
        # Base64 URL-safe encoding of 32 bytes = ~43 chars
        assert len(secret) > 40

    def test_secrets_are_unique(self):
        """Each generated secret should be unique."""
        from openhands.automation.webhook_router import _generate_webhook_secret

        secrets = [_generate_webhook_secret() for _ in range(100)]
        assert len(set(secrets)) == 100


class TestWebhookUrlGeneration:
    """Tests for webhook URL generation."""

    def test_url_format_with_base_url(self, monkeypatch):
        """Generated URLs use base_url when set."""
        from openhands.automation.webhook_router import _build_webhook_url

        monkeypatch.setenv("AUTOMATION_BASE_URL", "https://automation.example.com")

        # Clear cached settings
        from openhands.automation.config import clear_config_cache

        clear_config_cache()

        org_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        url = _build_webhook_url(org_id, "stripe")

        assert url == (
            "https://automation.example.com/api/automation/v1/events/"
            "12345678-1234-5678-1234-567812345678/stripe"
        )

        # Restore cache
        clear_config_cache()

    def test_url_fallback_to_localhost(self, monkeypatch):
        """Falls back to localhost when base_url not set."""
        from openhands.automation.webhook_router import _build_webhook_url

        monkeypatch.setenv("AUTOMATION_BASE_URL", "")
        monkeypatch.setenv("AUTOMATION_SERVER_PORT", "8000")

        # Clear cached settings
        from openhands.automation.config import clear_config_cache

        clear_config_cache()

        org_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        url = _build_webhook_url(org_id, "stripe")

        assert url == (
            "http://localhost:8000/api/automation/v1/events/"
            "12345678-1234-5678-1234-567812345678/stripe"
        )

        # Restore cache
        clear_config_cache()

    def test_url_trailing_slash_removed(self, monkeypatch):
        """Trailing slash in base URL should be removed."""
        from openhands.automation.webhook_router import _build_webhook_url

        monkeypatch.setenv("AUTOMATION_BASE_URL", "https://automation.example.com/")

        # Clear cached settings
        from openhands.automation.config import clear_config_cache

        clear_config_cache()

        org_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        url = _build_webhook_url(org_id, "stripe")

        # Should not have double slash
        assert "//" not in url.replace("https://", "")

        # Restore cache
        clear_config_cache()
