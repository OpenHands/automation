"""Tests for the provider descriptor registry and the signature verifiers.

Verifiers are constructed directly; the HTTP tests at the bottom cover what
only the wired-up path can show.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import FrozenInstanceError

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.config import Settings, clear_config_cache
from openhands.automation.event_schemas import WebhookEvent, parse_event
from openhands.automation.models import Automation, CustomWebhook
from openhands.automation.providers import (
    DEFAULT_BUILTIN_SIGNATURE_HEADER,
    DEFAULT_VERIFIER,
    PROVIDERS,
    SLACK_TIMESTAMP_HEADER,
    STANDARD_WEBHOOKS_ID_HEADER,
    STANDARD_WEBHOOKS_TIMESTAMP_HEADER,
    VERIFIERS,
    Capabilities,
    HmacSha256HexVerifier,
    Provider,
    SlackV0Verifier,
    StandardWebhooksVerifier,
    builtin_sources,
    get_header,
    get_provider,
    get_verifier,
    is_builtin_source,
    register_provider,
    reserved_sources,
    standard_webhooks_key,
    verifier_schemes,
    verify_signature,
)
from openhands.automation.schemas import (
    RESERVED_SOURCES,
    CustomWebhookCreate,
    CustomWebhookUpdate,
)
from openhands.automation.subjects import github_subject
from openhands.automation.utils.webhook import get_webhook_config


BUILTIN_PROVIDER_SOURCES = frozenset({"bitbucket_data_center", "github", "jira_dc"})

# Header bytes reach an app latin-1 decoded, so a signature header can carry a
# non-ASCII str. `hmac.compare_digest` raises on those rather than returning
# False, which would turn a bad signature into a 500.
NON_ASCII_SIGNATURE = b"\xff\xfe".decode("latin-1")


@pytest.fixture
def org_id(mock_authenticated_user: AuthenticatedUser) -> uuid.UUID:
    """Get org_id from the authenticated user fixture."""
    return mock_authenticated_user.org_id


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Clear the settings cache before and after each test."""
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def temp_provider():
    """Register a provider for one test; the registry is process-global."""
    registered: list[str] = []

    def _register(provider: Provider) -> Provider:
        register_provider(provider)
        registered.append(provider.source)
        return provider

    yield _register

    for source in registered:
        PROVIDERS.pop(source, None)


class TestGetHeader:
    """Header lookup has to survive whatever casing a caller hands us."""

    def test_exact_match(self):
        assert get_header({"X-Sig": "v"}, "X-Sig") == "v"

    def test_lowercase_stored(self):
        assert get_header({"x-sig": "v"}, "X-Sig") == "v"

    def test_uppercase_stored(self):
        assert get_header({"X-SIG": "v"}, "x-sig") == "v"

    def test_mixed_case_stored(self):
        assert get_header({"X-hUb-SiGnAtUrE-256": "v"}, "X-Hub-Signature-256") == "v"

    def test_missing(self):
        assert get_header({"other": "v"}, "X-Sig") is None

    def test_empty_mapping(self):
        assert get_header({}, "X-Sig") is None


class TestHmacSha256HexVerifier:
    """The scheme every existing source and custom webhook row uses today."""

    SECRET = "test-secret"
    HEADER = "X-Hub-Signature-256"

    def _sign(self, body: bytes, secret: str | None = None) -> str:
        digest = hmac.new(
            (secret or self.SECRET).encode(), body, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"

    def test_valid_prefixed_signature(self):
        body = b'{"a":1}'
        assert HmacSha256HexVerifier().verify(
            body=body,
            headers={self.HEADER: self._sign(body)},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_valid_bare_hex_signature(self):
        """Linear sends the digest with no 'sha256=' prefix."""
        body = b'{"a":1}'
        bare = self._sign(body).removeprefix("sha256=")
        assert HmacSha256HexVerifier().verify(
            body=body,
            headers={self.HEADER: bare},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_tampered_body_rejected(self):
        signature = self._sign(b'{"a":1}')
        assert not HmacSha256HexVerifier().verify(
            body=b'{"a":2}',
            headers={self.HEADER: signature},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_wrong_secret_rejected(self):
        body = b'{"a":1}'
        assert not HmacSha256HexVerifier().verify(
            body=body,
            headers={self.HEADER: self._sign(body, "other-secret")},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_missing_header_rejected(self):
        assert not HmacSha256HexVerifier().verify(
            body=b'{"a":1}',
            headers={},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_non_ascii_signature_rejected(self):
        """Starlette decodes header bytes as latin-1, so this is reachable."""
        assert not HmacSha256HexVerifier().verify(
            body=b'{"a":1}',
            headers={self.HEADER: NON_ASCII_SIGNATURE},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_reads_the_configured_header_not_a_fixed_one(self):
        """A custom webhook may put the same scheme behind any header name."""
        body = b'{"a":1}'
        signature = self._sign(body)
        verifier = HmacSha256HexVerifier()
        assert verifier.verify(
            body=body,
            headers={"Stripe-Signature": signature},
            secret=self.SECRET,
            signature_header="Stripe-Signature",
        )
        assert not verifier.verify(
            body=body,
            headers={"Stripe-Signature": signature},
            secret=self.SECRET,
            signature_header="X-Signature-256",
        )

    def test_matches_verify_signature_directly(self):
        """The verifier is the existing function, not a reimplementation."""
        body = b'{"a":1}'
        signature = self._sign(body)
        assert verify_signature(body, signature, self.SECRET)
        assert HmacSha256HexVerifier().verify(
            body=body,
            headers={self.HEADER: signature},
            secret=self.SECRET,
            signature_header=self.HEADER,
        )


class TestStandardWebhooksVerifier:
    """standardwebhooks.com -- GitLab 19.1+ signing tokens, Svix."""

    SECRET = "whsec_uX0GkGuuABEuIJhcJxNEotfG8+WjeIkZuJJqJdsm/CQ="
    MSG_ID = "msg_2abc"
    TS = "1752900000"
    NOW = 1752900000
    HEADER = "webhook-signature"

    def _verifier(self, now: int | None = None) -> StandardWebhooksVerifier:
        return StandardWebhooksVerifier(clock=lambda: self.NOW if now is None else now)

    def _sign(
        self,
        body: bytes,
        secret: str | None = None,
        msg_id: str | None = None,
        ts: str | None = None,
    ) -> str:
        key = standard_webhooks_key(secret or self.SECRET)
        signed = f"{msg_id or self.MSG_ID}.{ts or self.TS}.".encode() + body
        digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
        return f"v1,{digest.decode()}"

    def _headers(self, signature: str, ts: str | None = None) -> dict[str, str]:
        return {
            self.HEADER: signature,
            STANDARD_WEBHOOKS_ID_HEADER: self.MSG_ID,
            STANDARD_WEBHOOKS_TIMESTAMP_HEADER: ts or self.TS,
        }

    def _verify(self, verifier, body: bytes, headers: dict[str, str]) -> bool:
        return verifier.verify(
            body=body,
            headers=headers,
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_known_good_fixture(self):
        body = b'{"object_kind":"note"}'
        assert self._verify(self._verifier(), body, self._headers(self._sign(body)))

    def test_key_is_base64_decoded_after_stripping_whsec(self):
        """The signing key is the decoded secret, not its literal characters."""
        assert standard_webhooks_key(self.SECRET) == base64.b64decode(
            self.SECRET.removeprefix("whsec_")
        )

    def test_non_base64_secret_falls_back_to_raw_bytes(self):
        assert standard_webhooks_key("plain-text-secret") == b"plain-text-secret"

    def test_a_plain_secret_that_parses_as_base64_is_still_decoded(self):
        """The scheme defines the secret as base64, so this is spec, not a bug.

        Worth pinning: an operator who types a literal secret that happens to
        be valid base64 gets a key that is not the characters they typed.
        """
        assert standard_webhooks_key("abcdefgh") == base64.b64decode("abcdefgh")
        assert standard_webhooks_key("abcdefgh") != b"abcdefgh"

    def test_non_ascii_signature_rejected(self):
        body = b'{"object_kind":"note"}'
        assert not self._verify(
            self._verifier(), body, self._headers(f"v1,{NON_ASCII_SIGNATURE}")
        )

    def test_tampered_body_rejected(self):
        signature = self._sign(b'{"object_kind":"note"}')
        assert not self._verify(
            self._verifier(), b'{"object_kind":"push"}', self._headers(signature)
        )

    def test_wrong_key_rejected(self):
        body = b'{"object_kind":"note"}'
        signature = self._sign(
            body, secret="whsec_" + base64.b64encode(b"x" * 24).decode()
        )
        assert not self._verify(self._verifier(), body, self._headers(signature))

    def test_msg_id_is_signed(self):
        """The id is part of the signed content, so swapping it must fail."""
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body, msg_id="msg_other"))
        assert not self._verify(self._verifier(), body, headers)

    def test_timestamp_past_the_window_rejected(self):
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body))
        assert not self._verify(self._verifier(now=self.NOW + 301), body, headers)

    def test_timestamp_inside_the_window_accepted(self):
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body))
        assert self._verify(self._verifier(now=self.NOW + 299), body, headers)

    def test_timestamp_from_the_future_rejected(self):
        """The window is absolute, so a forward-dated delivery fails too."""
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body))
        assert not self._verify(self._verifier(now=self.NOW - 301), body, headers)

    def test_timestamp_is_signed(self):
        """Re-stamping a captured delivery to make it fresh must not verify."""
        body = b'{"object_kind":"note"}'
        stale = self._sign(body, ts=str(self.NOW - 10_000))
        assert not self._verify(
            self._verifier(), body, self._headers(stale, ts=str(self.NOW))
        )

    def test_non_integer_timestamp_rejected(self):
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body), ts="not-a-number")
        assert not self._verify(self._verifier(), body, headers)

    def test_missing_id_header_rejected(self):
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body))
        del headers[STANDARD_WEBHOOKS_ID_HEADER]
        assert not self._verify(self._verifier(), body, headers)

    def test_missing_timestamp_header_rejected(self):
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body))
        del headers[STANDARD_WEBHOOKS_TIMESTAMP_HEADER]
        assert not self._verify(self._verifier(), body, headers)

    def test_missing_signature_header_rejected(self):
        body = b'{"object_kind":"note"}'
        headers = self._headers(self._sign(body))
        del headers[self.HEADER]
        assert not self._verify(self._verifier(), body, headers)

    def test_multiple_signatures_any_match_accepted(self):
        """Rotation sends both old and new; one match is enough."""
        body = b'{"object_kind":"note"}'
        good = self._sign(body)
        stale_key = "whsec_" + base64.b64encode(b"y" * 24).decode()
        bad = self._sign(body, secret=stale_key)
        assert self._verify(self._verifier(), body, self._headers(f"{bad} {good}"))
        assert self._verify(self._verifier(), body, self._headers(f"{good} {bad}"))

    def test_unknown_version_ignored(self):
        body = b'{"object_kind":"note"}'
        v2 = "v2," + self._sign(body).partition(",")[2]
        assert not self._verify(self._verifier(), body, self._headers(v2))

    def test_bare_signature_without_version_rejected(self):
        """Unlike hmac_sha256_hex, this scheme requires the version tag."""
        body = b'{"object_kind":"note"}'
        bare = self._sign(body).partition(",")[2]
        assert not self._verify(self._verifier(), body, self._headers(bare))


class TestSlackV0Verifier:
    """Slack Events API: hex HMAC over "v0:{timestamp}:{body}"."""

    SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
    TS = "1531420618"
    NOW = 1531420618
    HEADER = "X-Slack-Signature"

    def _verifier(self, now: int | None = None) -> SlackV0Verifier:
        return SlackV0Verifier(clock=lambda: self.NOW if now is None else now)

    def _sign(self, body: bytes, ts: str | None = None) -> str:
        signed = b"v0:" + (ts or self.TS).encode() + b":" + body
        return (
            "v0=" + hmac.new(self.SECRET.encode(), signed, hashlib.sha256).hexdigest()
        )

    def _headers(self, signature: str, ts: str | None = None) -> dict[str, str]:
        return {self.HEADER: signature, SLACK_TIMESTAMP_HEADER: ts or self.TS}

    def _verify(self, verifier, body: bytes, headers: dict[str, str]) -> bool:
        return verifier.verify(
            body=body,
            headers=headers,
            secret=self.SECRET,
            signature_header=self.HEADER,
        )

    def test_known_good_fixture(self):
        body = b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J"
        assert self._verify(self._verifier(), body, self._headers(self._sign(body)))

    def test_tampered_body_rejected(self):
        signature = self._sign(b"team_id=T1DC2JH3J")
        assert not self._verify(
            self._verifier(), b"team_id=TEVIL", self._headers(signature)
        )

    def test_stale_timestamp_rejected(self):
        body = b"team_id=T1DC2JH3J"
        headers = self._headers(self._sign(body))
        assert not self._verify(self._verifier(now=self.NOW + 301), body, headers)

    def test_fresh_timestamp_accepted(self):
        body = b"team_id=T1DC2JH3J"
        headers = self._headers(self._sign(body))
        assert self._verify(self._verifier(now=self.NOW + 299), body, headers)

    def test_timestamp_is_signed(self):
        body = b"team_id=T1DC2JH3J"
        stale = self._sign(body, ts=str(self.NOW - 10_000))
        assert not self._verify(
            self._verifier(), body, self._headers(stale, ts=str(self.NOW))
        )

    def test_missing_timestamp_header_rejected(self):
        body = b"team_id=T1DC2JH3J"
        assert not self._verify(self._verifier(), body, {self.HEADER: self._sign(body)})

    def test_missing_signature_header_rejected(self):
        assert not self._verify(
            self._verifier(), b"team_id=T1DC2JH3J", {SLACK_TIMESTAMP_HEADER: self.TS}
        )

    def test_non_ascii_signature_rejected(self):
        assert not self._verify(
            self._verifier(),
            b"team_id=T1DC2JH3J",
            self._headers(NON_ASCII_SIGNATURE),
        )

    def test_non_integer_timestamp_rejected(self):
        body = b"team_id=T1DC2JH3J"
        headers = self._headers(self._sign(body), ts="nope")
        assert not self._verify(self._verifier(), body, headers)

    def test_v0_prefix_required(self):
        body = b"team_id=T1DC2JH3J"
        bare = self._sign(body).removeprefix("v0=")
        assert not self._verify(self._verifier(), body, self._headers(bare))


class TestVerifierRegistry:
    def test_registered_schemes(self):
        assert verifier_schemes() == {
            "hmac_sha256_hex",
            "standard_webhooks",
            "slack_v0",
        }

    def test_default_scheme_is_the_existing_behaviour(self):
        assert DEFAULT_VERIFIER == "hmac_sha256_hex"
        assert isinstance(VERIFIERS[DEFAULT_VERIFIER], HmacSha256HexVerifier)

    def test_none_resolves_to_the_default(self):
        """A row whose scheme was cleared must not lose its verifier."""
        assert get_verifier(None) is VERIFIERS[DEFAULT_VERIFIER]

    def test_named_scheme_resolves(self):
        assert isinstance(get_verifier("slack_v0"), SlackV0Verifier)
        assert isinstance(get_verifier("standard_webhooks"), StandardWebhooksVerifier)

    def test_unknown_scheme_resolves_to_nothing(self):
        assert get_verifier("pgp") is None


class TestProviderRegistry:
    def test_builtin_providers_registered(self):
        assert set(builtin_sources()) == BUILTIN_PROVIDER_SOURCES

    def test_is_builtin_source(self):
        assert is_builtin_source("github")
        assert not is_builtin_source("stripe")

    def test_reserved_sources_are_derived_from_the_registry(self):
        assert reserved_sources() == frozenset(PROVIDERS)
        assert RESERVED_SOURCES == BUILTIN_PROVIDER_SOURCES

    def test_registering_a_provider_reserves_its_source(self, temp_provider):
        assert not is_builtin_source("gitlab")
        temp_provider(Provider(source="gitlab", parse=lambda p: parse_event("x", p)))
        assert is_builtin_source("gitlab")
        assert "gitlab" in reserved_sources()

    def test_a_late_registered_source_cannot_be_claimed_by_a_custom_webhook(
        self, temp_provider
    ):
        """Validation asks the registry, not a snapshot taken at import time."""
        temp_provider(Provider(source="gitlab", parse=lambda p: parse_event("x", p)))
        with pytest.raises(ValueError, match="reserved source name"):
            CustomWebhookCreate(name="GitLab", source="gitlab")

    def test_builtin_sources_cannot_be_claimed_by_a_custom_webhook(self):
        for source in sorted(BUILTIN_PROVIDER_SOURCES):
            with pytest.raises(ValueError, match="reserved source name"):
                CustomWebhookCreate(name="Impostor", source=source)


class TestProviderDescriptors:
    def test_builtins_default_to_the_existing_verifier_and_header(self):
        for source in sorted(BUILTIN_PROVIDER_SOURCES):
            provider = get_provider(source)
            assert provider is not None
            assert provider.verifier == DEFAULT_VERIFIER
            assert provider.signature_header == DEFAULT_BUILTIN_SIGNATURE_HEADER

    def test_builtins_read_the_shared_webhook_secret(self):
        settings = Settings(webhook_secret="shared")
        for source in sorted(BUILTIN_PROVIDER_SOURCES):
            provider = get_provider(source)
            assert provider is not None
            assert provider.secret_from_settings is not None
            assert provider.secret_from_settings(settings) == "shared"

    def test_an_empty_shared_secret_reads_as_absent(self):
        """An unset secret must be None, not "", or the source looks configured."""
        provider = get_provider("github")
        assert provider is not None
        assert provider.secret_from_settings is not None
        assert provider.secret_from_settings(Settings(webhook_secret="")) is None

    def test_parse_event_delegates_to_the_descriptor(self):
        """There is one parser registry now, and it is the provider registry."""
        payload = {
            "ref": "refs/heads/main",
            "before": "abc123",
            "after": "def456",
            "commits": [],
            "repository": {
                "id": 123,
                "name": "test-repo",
                "full_name": "org/test-repo",
                "private": False,
            },
            "sender": {"id": 1, "login": "testuser"},
        }
        provider = get_provider("github")
        assert provider is not None
        assert parse_event("github", payload).event_key == "push"
        assert parse_event("github", payload).event_key == (
            provider.parse(payload).event_key
        )

    def test_unregistered_source_still_falls_back_to_a_custom_event(self):
        event = parse_event("stripe", {"type": "payment.completed"})
        assert event.source == "stripe"
        assert event.event_key == "payment.completed"

    def test_capabilities_default_is_conservative(self):
        """Multi-connection safety is declared per provider, never assumed."""
        assert Capabilities().tolerates_multiple_connections is False
        for source in sorted(BUILTIN_PROVIDER_SOURCES):
            provider = get_provider(source)
            assert provider is not None
            assert provider.capabilities.tolerates_multiple_connections is False

    def test_only_github_names_its_deliveries(self):
        """`event_id_header` is opt-in, and only GitHub sends one today."""
        github = get_provider("github")
        assert github is not None
        assert github.event_id_header == "X-GitHub-Delivery"

        for source in sorted(BUILTIN_PROVIDER_SOURCES - {"github"}):
            provider = get_provider(source)
            assert provider is not None
            assert provider.event_id_header is None

    def test_handshake_is_unset_on_every_builtin(self):
        """The hook is reserved and deliberately unwired, not forgotten."""
        for source in sorted(BUILTIN_PROVIDER_SOURCES):
            provider = get_provider(source)
            assert provider is not None
            assert provider.handshake is None

    def test_only_github_names_a_subject(self):
        """The others are not shut out; their triggers use `subject_key_expr`."""
        github = get_provider("github")
        assert github is not None
        assert github.subject is github_subject

        for source in sorted(BUILTIN_PROVIDER_SOURCES - {"github"}):
            provider = get_provider(source)
            assert provider is not None
            assert provider.subject is None

    def test_descriptors_are_frozen(self):
        provider = get_provider("github")
        assert provider is not None
        with pytest.raises(FrozenInstanceError):
            provider.verifier = "slack_v0"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            Capabilities().tolerates_multiple_connections = True  # type: ignore[misc]


class TestSignatureSchemeValidation:
    def test_create_defaults_to_the_existing_scheme(self):
        assert CustomWebhookCreate(name="W", source="w").signature_scheme == (
            DEFAULT_VERIFIER
        )

    def test_create_accepts_every_registered_scheme(self):
        for scheme in sorted(verifier_schemes()):
            created = CustomWebhookCreate(name="W", source="w", signature_scheme=scheme)
            assert created.signature_scheme == scheme

    def test_create_rejects_an_unregistered_scheme(self):
        with pytest.raises(ValueError, match="Invalid signature_scheme"):
            CustomWebhookCreate(name="W", source="w", signature_scheme="pgp")

    def test_update_leaves_the_scheme_alone_when_omitted(self):
        assert CustomWebhookUpdate().signature_scheme is None
        assert "signature_scheme" not in CustomWebhookUpdate(name="W").model_dump(
            exclude_unset=True
        )

    def test_update_rejects_an_unregistered_scheme(self):
        with pytest.raises(ValueError, match="Invalid signature_scheme"):
            CustomWebhookUpdate(signature_scheme="pgp")

    def test_update_accepts_a_registered_scheme(self):
        assert CustomWebhookUpdate(signature_scheme="slack_v0").signature_scheme == (
            "slack_v0"
        )


class TestGetWebhookConfigScheme:
    @pytest.mark.asyncio
    async def test_builtin_config_comes_from_the_descriptor(
        self, async_session, org_id, monkeypatch
    ):
        monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", "shared")
        config = await get_webhook_config("github", org_id, async_session)
        assert config is not None
        assert config.is_builtin is True
        assert config.secret == "shared"
        assert config.signature_header == DEFAULT_BUILTIN_SIGNATURE_HEADER
        assert config.signature_scheme == DEFAULT_VERIFIER

    @pytest.mark.asyncio
    async def test_custom_webhook_scheme_is_threaded_through(
        self, async_session, org_id
    ):
        async_session.add(
            CustomWebhook(
                org_id=org_id,
                name="GitLab",
                source="gitlab-ee",
                webhook_secret="whsec_abc",
                event_key_expr="object_kind",
                signature_header="webhook-signature",
                signature_scheme="standard_webhooks",
            )
        )
        await async_session.commit()

        config = await get_webhook_config("gitlab-ee", org_id, async_session)
        assert config is not None
        assert config.signature_scheme == "standard_webhooks"
        assert config.signature_header == "webhook-signature"

    @pytest.mark.asyncio
    async def test_a_row_with_a_cleared_scheme_reads_as_the_default(
        self, async_session, org_id
    ):
        """Migration 017 backfills, so NULL comes from a PATCH, not from age."""
        webhook = CustomWebhook(
            org_id=org_id,
            name="Legacy",
            source="legacy",
            webhook_secret="s3cret-value",
        )
        async_session.add(webhook)
        await async_session.commit()

        await async_session.execute(
            text("UPDATE custom_webhooks SET signature_scheme = NULL WHERE id = :id"),
            {"id": webhook.id},
        )
        await async_session.commit()
        async_session.expire_all()

        config = await get_webhook_config("legacy", org_id, async_session)
        assert config is not None
        assert config.signature_scheme == DEFAULT_VERIFIER


def _make_automation(org_id: uuid.UUID, user_id: uuid.UUID, source: str) -> Automation:
    return Automation(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=org_id,
        name=f"On {source}",
        tarball_path="oh-internal://uploads/test.tar.gz",
        entrypoint="python main.py",
        trigger={"type": "event", "source": source, "on": "*"},
    )


@pytest.mark.asyncio
async def test_legacy_custom_webhook_still_verifies_unchanged(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
    mock_authenticated_user: AuthenticatedUser,
):
    """A row with a NULL scheme verifies exactly as it did before this change."""
    webhook = CustomWebhook(
        org_id=org_id,
        name="Legacy",
        source="legacy",
        webhook_secret="s3cret-value",
        event_key_expr="type",
        signature_header="X-Signature-256",
    )
    async_session.add(webhook)
    async_session.add(
        _make_automation(org_id, mock_authenticated_user.user_id, "legacy")
    )
    await async_session.commit()

    await async_session.execute(
        text("UPDATE custom_webhooks SET signature_scheme = NULL WHERE id = :id"),
        {"id": webhook.id},
    )
    await async_session.commit()
    # The request shares this session, so drop the instance the identity map is
    # still holding -- otherwise the endpoint reads the pre-UPDATE value.
    async_session.expire_all()

    body = json.dumps({"type": "order.created"}).encode()
    digest = hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/legacy",
        content=body,
        headers={
            "X-Signature-256": f"sha256={digest}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["matched"] == 1


@pytest.mark.asyncio
async def test_custom_webhook_with_standard_webhooks_scheme(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
    mock_authenticated_user: AuthenticatedUser,
):
    """The acceptance criterion: a non-default scheme, configured and honoured."""
    secret = "whsec_" + base64.b64encode(b"gitlab-signing-key-000").decode()
    async_session.add(
        CustomWebhook(
            org_id=org_id,
            name="GitLab",
            source="gitlab-ee",
            webhook_secret=secret,
            event_key_expr="object_kind",
            signature_header="webhook-signature",
            signature_scheme="standard_webhooks",
        )
    )
    async_session.add(
        _make_automation(org_id, mock_authenticated_user.user_id, "gitlab-ee")
    )
    await async_session.commit()

    body = json.dumps({"object_kind": "note"}).encode()
    now = str(int(time.time()))
    key = standard_webhooks_key(secret)
    signed = f"msg_1.{now}.".encode() + body
    signature = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/gitlab-ee",
        content=body,
        headers={
            "webhook-signature": f"v1,{signature}",
            "webhook-id": "msg_1",
            "webhook-timestamp": now,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["matched"] == 1


@pytest.mark.asyncio
async def test_standard_webhooks_rejects_a_replayed_delivery(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
):
    """A genuinely signed delivery, replayed outside the window, is refused."""
    secret = "whsec_" + base64.b64encode(b"gitlab-signing-key-000").decode()
    async_session.add(
        CustomWebhook(
            org_id=org_id,
            name="GitLab",
            source="gitlab-ee",
            webhook_secret=secret,
            event_key_expr="object_kind",
            signature_header="webhook-signature",
            signature_scheme="standard_webhooks",
        )
    )
    await async_session.commit()

    body = json.dumps({"object_kind": "note"}).encode()
    stale = str(int(time.time()) - 10_000)
    key = standard_webhooks_key(secret)
    signed = f"msg_1.{stale}.".encode() + body
    signature = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/gitlab-ee",
        content=body,
        headers={
            "webhook-signature": f"v1,{signature}",
            "webhook-id": "msg_1",
            "webhook-timestamp": stale,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid signature"


class _GitLabEvent(WebhookEvent):
    """Stands in for a real provider's event class, so the wiring is tested."""

    object_kind: str = ""

    @property
    def source(self) -> str:
        return "gitlab"

    @property
    def event_key(self) -> str:
        return self.object_kind


@pytest.mark.asyncio
async def test_adding_a_provider_takes_a_registry_entry_and_nothing_else(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
    mock_authenticated_user: AuthenticatedUser,
    monkeypatch: pytest.MonkeyPatch,
    temp_provider,
):
    """One descriptor routes a brand-new source end to end.

    No change to `event_router.py`, `utils/webhook.py` or `schemas.py` was
    needed to make this pass.
    """
    monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", "shared")
    temp_provider(
        Provider(
            source="gitlab",
            parse=lambda payload: _GitLabEvent(**payload),
            verifier="slack_v0",
            signature_header="X-Slack-Signature",
            secret_from_settings=lambda s: s.webhook_secret or None,
            capabilities=Capabilities(tolerates_multiple_connections=True),
        )
    )

    async_session.add(
        _make_automation(org_id, mock_authenticated_user.user_id, "gitlab")
    )
    await async_session.commit()

    body = json.dumps({"payload": {"object_kind": "merge_request"}}).encode()
    now = str(int(time.time()))
    signed = b"v0:" + now.encode() + b":" + body
    digest = hmac.new(b"shared", signed, hashlib.sha256).hexdigest()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/gitlab",
        content=body,
        headers={
            "X-Slack-Signature": f"v0={digest}",
            "X-Slack-Request-Timestamp": now,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["matched"] == 1

    # The descriptor's capability flag is what phase 3 will read.
    provider = get_provider("gitlab")
    assert provider is not None
    assert provider.capabilities.tolerates_multiple_connections is True


@pytest.mark.asyncio
async def test_a_scheme_with_no_verifier_refuses_the_delivery(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
):
    """A scheme this build cannot implement is a 500, not a silent fallback."""
    webhook = CustomWebhook(
        org_id=org_id,
        name="Exotic",
        source="exotic",
        webhook_secret="s3cret-value",
        signature_header="X-Signature-256",
    )
    async_session.add(webhook)
    await async_session.commit()

    await async_session.execute(
        text("UPDATE custom_webhooks SET signature_scheme = 'pgp' WHERE id = :id"),
        {"id": webhook.id},
    )
    await async_session.commit()
    async_session.expire_all()

    body = json.dumps({"type": "x"}).encode()
    digest = hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/exotic",
        content=body,
        headers={
            "X-Signature-256": f"sha256={digest}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_a_non_ascii_signature_header_is_refused_not_a_crash(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
):
    """A garbage signature must be a 401, not an unhandled TypeError."""
    webhook = CustomWebhook(
        org_id=org_id,
        name="Legacy",
        source="legacy",
        webhook_secret="s3cret-value",
        signature_header="X-Signature-256",
    )
    async_session.add(webhook)
    await async_session.commit()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/legacy",
        content=json.dumps({"type": "order.created"}).encode(),
        headers=[
            (b"X-Signature-256", b"\xff\xfe"),
            (b"Content-Type", b"application/json"),
        ],
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid signature"


@pytest.mark.asyncio
async def test_patching_the_scheme_changes_what_verifies(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
    mock_authenticated_user: AuthenticatedUser,
):
    """The PATCH persists, and the delivery path honours the new scheme."""
    webhook = CustomWebhook(
        org_id=org_id,
        name="Switcher",
        source="switcher",
        webhook_secret="s3cret-value",
        event_key_expr="type",
        signature_header="X-Signature-256",
    )
    async_session.add(webhook)
    async_session.add(
        _make_automation(org_id, mock_authenticated_user.user_id, "switcher")
    )
    await async_session.commit()

    patched = await async_client.patch(
        f"/api/automation/v1/webhooks/{webhook.id}",
        json={"signature_scheme": "slack_v0"},
    )
    assert patched.status_code == 200
    assert patched.json()["signature_scheme"] == "slack_v0"
    async_session.expire_all()

    body = json.dumps({"type": "order.created"}).encode()

    # The scheme it used to verify with is no longer accepted.
    stale = await async_client.post(
        f"/api/automation/v1/events/{org_id}/switcher",
        content=body,
        headers={
            "X-Signature-256": (
                "sha256=" + hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()
            ),
            "Content-Type": "application/json",
        },
    )
    assert stale.status_code == 401

    now = str(int(time.time()))
    signed = b"v0:" + now.encode() + b":" + body
    digest = hmac.new(b"s3cret-value", signed, hashlib.sha256).hexdigest()
    accepted = await async_client.post(
        f"/api/automation/v1/events/{org_id}/switcher",
        content=body,
        headers={
            "X-Signature-256": f"v0={digest}",
            "X-Slack-Request-Timestamp": now,
            "Content-Type": "application/json",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["matched"] == 1
