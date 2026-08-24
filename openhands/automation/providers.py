"""Provider descriptors and signature verifiers.

One registry describes an event provider: how its payloads parse into a typed
`WebhookEvent`, how a delivery's signature is verified, where its shared secret
comes from, and what its transport tolerates. Before this module the same
provider was described in two unrelated places -- ``BUILTIN_SOURCES`` in
``utils/webhook.py`` (secret extraction) and the ``event_schemas`` parser
registry -- with ``RESERVED_SOURCES`` hand-maintained alongside them as a third.

Verification is a registry too. ``verify_signature()`` was the only scheme the
service could speak: hex HMAC-SHA256 over the raw body. Providers that sign
something else -- Standard Webhooks (GitLab 19.1+, Svix), Slack's
``v0:{ts}:{body}`` -- could not be onboarded at all. ``VERIFIERS`` resolves a
scheme by name so a provider, or a per-org custom webhook, can pick one.

This module is deliberately free of transport imports so it can be read by a
non-HTTP transport; the one HTTP-shaped field (``handshake``) is reserved and
typed under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from openhands.automation.config import Settings
from openhands.automation.event_schemas import WebhookEvent


if TYPE_CHECKING:
    from fastapi import Request, Response

    from openhands.automation.ingest import EventSubject


logger = logging.getLogger("automation.providers")


ParseFunc = Callable[[dict[str, Any]], WebhookEvent]
SecretFunc = Callable[[Settings], str | None]
SubjectFunc = Callable[[dict[str, Any]], "EventSubject | None"]
HandshakeFunc = Callable[["Request"], "Response | None"]

# Scheme used when nothing says otherwise: the behaviour every existing source
# and every existing custom_webhooks row has today.
DEFAULT_VERIFIER = "hmac_sha256_hex"

# The header built-in sources sign into. They are all forwarded by the OpenHands
# server, which reuses GitHub's header name regardless of the upstream provider.
DEFAULT_BUILTIN_SIGNATURE_HEADER = "X-Hub-Signature-256"


# =============================================================================
# Header access
# =============================================================================


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Look up a header case-insensitively.

    Starlette's ``Headers`` is already case-insensitive, but a non-HTTP caller
    or a test may pass a plain dict, and header casing is not something a
    verifier should have to guess at.
    """
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    value = headers.get(lowered)
    if value is not None:
        return value
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return candidate
    return None


def _now_seconds() -> int:
    return int(time.time())


# =============================================================================
# Verifiers
# =============================================================================


class WebhookVerifier(Protocol):
    """Proves a raw delivery was signed by the holder of the shared secret.

    ``signature_header`` is the header the signature itself is carried in. It
    is a parameter rather than a property of the verifier because custom
    webhooks configure it per row, and the same scheme is used behind different
    header names by different vendors. Everything *else* a scheme needs -- a
    message id, a timestamp -- the verifier reads from ``headers`` itself,
    since those header names are fixed by the scheme, not by the operator.
    """

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
        signature_header: str,
    ) -> bool: ...


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify HMAC-SHA256 signature.

    Accepts both formats:
    - GitHub/normalized: 'sha256=<hex>'
    - Raw hex digest: '<hex>' (e.g., Linear)

    Args:
        payload: Raw request body bytes
        signature: Signature from header
        secret: The shared secret

    Returns:
        True if signature is valid
    """
    # Normalize: strip 'sha256=' prefix if present
    if signature.startswith("sha256="):
        signature = signature[7:]

    computed = hmac.new(
        secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature)


@dataclass(frozen=True, slots=True)
class HmacSha256HexVerifier:
    """GitHub/Linear style: hex HMAC-SHA256 over the raw body."""

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
        signature_header: str,
    ) -> bool:
        signature = get_header(headers, signature_header)
        if not signature:
            return False
        return verify_signature(body, signature, secret)


# Standard Webhooks (https://www.standardwebhooks.com) -- used by GitLab 19.1+
# signing tokens, Svix, and others. Signature = base64(HMAC-SHA256(key, msg))
# where msg = "{id}.{timestamp}.{body}" and key = base64decode(secret w/o whsec_).
STANDARD_WEBHOOKS_TOLERANCE_SECONDS = 300
STANDARD_WEBHOOKS_ID_HEADER = "webhook-id"
STANDARD_WEBHOOKS_TIMESTAMP_HEADER = "webhook-timestamp"


def standard_webhooks_key(secret: str) -> bytes:
    """Derive the signing key from a Standard Webhooks secret.

    Secrets are conventionally "whsec_<base64>": strip the prefix and
    base64-decode. Fall back to raw UTF-8 bytes if not valid base64.
    """
    key_material = secret
    if key_material.startswith("whsec_"):
        key_material = key_material[len("whsec_") :]
    try:
        return base64.b64decode(key_material, validate=True)
    except (binascii.Error, ValueError):
        return secret.encode("utf-8")


@dataclass(frozen=True, slots=True)
class StandardWebhooksVerifier:
    """standardwebhooks.com: base64 HMAC over "{id}.{timestamp}.{body}".

    The timestamp is signed, so checking it against a freshness window is real
    replay protection rather than a formality -- an attacker cannot re-stamp a
    captured delivery without the secret.
    """

    tolerance_seconds: int = STANDARD_WEBHOOKS_TOLERANCE_SECONDS
    clock: Callable[[], int] = field(default=_now_seconds)

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
        signature_header: str,
    ) -> bool:
        signature = get_header(headers, signature_header)
        msg_id = get_header(headers, STANDARD_WEBHOOKS_ID_HEADER)
        timestamp = get_header(headers, STANDARD_WEBHOOKS_TIMESTAMP_HEADER)
        if not signature or not msg_id or not timestamp:
            return False

        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(self.clock() - sent_at) > self.tolerance_seconds:
            return False

        key = standard_webhooks_key(secret)
        signed_content = f"{msg_id}.{timestamp}.".encode() + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode("utf-8")

        # The header carries a space-separated list so a secret can be rotated
        # without a gap; any one match is enough. Every token is compared even
        # after a match, to keep the work independent of where the match sits.
        matched = False
        for token in signature.split():
            # Each token is "v{version},{signature}"; we support v1.
            version, _, candidate = token.partition(",")
            if not candidate or version != "v1":
                continue
            if hmac.compare_digest(candidate, expected):
                matched = True
        return matched


# Slack's Events API signs "v0:{timestamp}:{body}" and sends the hex digest as
# "v0=<hex>". Slack's own guidance is to reject deliveries older than 5 minutes.
SLACK_TOLERANCE_SECONDS = 300
SLACK_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"


@dataclass(frozen=True, slots=True)
class SlackV0Verifier:
    """Slack Events API: hex HMAC over "v0:{timestamp}:{body}"."""

    tolerance_seconds: int = SLACK_TOLERANCE_SECONDS
    clock: Callable[[], int] = field(default=_now_seconds)

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
        signature_header: str,
    ) -> bool:
        signature = get_header(headers, signature_header)
        timestamp = get_header(headers, SLACK_TIMESTAMP_HEADER)
        if not signature or not timestamp:
            return False

        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(self.clock() - sent_at) > self.tolerance_seconds:
            return False

        signed_content = b"v0:" + timestamp.encode("utf-8") + b":" + body
        expected = (
            "v0="
            + hmac.new(
                secret.encode("utf-8"), signed_content, hashlib.sha256
            ).hexdigest()
        )
        return hmac.compare_digest(signature, expected)


VERIFIERS: dict[str, WebhookVerifier] = {
    "hmac_sha256_hex": HmacSha256HexVerifier(),
    "standard_webhooks": StandardWebhooksVerifier(),
    "slack_v0": SlackV0Verifier(),
}


def get_verifier(scheme: str | None) -> WebhookVerifier | None:
    """Resolve a signature scheme name, treating None as the default."""
    return VERIFIERS.get(scheme or DEFAULT_VERIFIER)


def verifier_schemes() -> frozenset[str]:
    """Every signature scheme a webhook may be configured with."""
    return frozenset(VERIFIERS)


# =============================================================================
# Provider descriptors
# =============================================================================


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a provider's transport tolerates. Declared, never assumed."""

    # Whether the provider load-balances a payload across the simultaneous
    # connections an app holds, rather than broadcasting it to all of them.
    # Slack does the former and endorses up to 10 connections, so Socket Mode
    # is safe to run active-active; an arbitrary WebSocket upstream makes no
    # such promise and would duplicate every event per replica. Read by #360.
    tolerates_multiple_connections: bool = False


@dataclass(frozen=True, slots=True)
class Provider:
    """Everything the service knows about one event provider."""

    source: str
    parse: ParseFunc
    verifier: str = DEFAULT_VERIFIER
    signature_header: str = DEFAULT_BUILTIN_SIGNATURE_HEADER
    secret_from_settings: SecretFunc | None = None
    # Header carrying the provider's own id for a delivery, used to drop
    # redeliveries (#361). None means the provider does not identify its
    # deliveries, and its events are recorded but never deduplicated.
    #
    # It names a header rather than a payload path because HTTP is the only
    # transport that has to go looking: a stream transport is handed the
    # envelope and sets `AcceptedEvent.provider_event_id` from it directly.
    event_id_header: str | None = None
    # Reserved for #362 (subject -> conversation routing). Nothing reads it.
    subject: SubjectFunc | None = None
    # Reserved. Deliberately not wired: no provider on the roadmap performs an
    # HTTP handshake. Slack's Events API challenge was the motivating example,
    # and #360 makes Socket Mode our Slack path, where it does not apply. See
    # the "On the handshake hook" note in #359 -- this is the explicit decision
    # to defer, not an oversight.
    handshake: HandshakeFunc | None = None
    capabilities: Capabilities = Capabilities()


PROVIDERS: dict[str, Provider] = {}


def register_provider(provider: Provider) -> None:
    """Register a provider descriptor, replacing any entry for the same source."""
    PROVIDERS[provider.source] = provider


def get_provider(source: str) -> Provider | None:
    """Return the descriptor for a source, or None if it is not built in."""
    return PROVIDERS.get(source)


def is_builtin_source(source: str) -> bool:
    """Check if a source is a builtin integration."""
    return source in PROVIDERS


def builtin_sources() -> list[str]:
    """Every registered provider source, sorted."""
    return sorted(PROVIDERS)


def reserved_sources() -> frozenset[str]:
    """Source names a custom webhook may not claim.

    Derived from the registry rather than maintained separately, so a provider
    cannot be added without also being reserved.
    """
    return frozenset(PROVIDERS)


def _register_builtin_providers() -> None:
    """Register the built-in providers. Called at module load."""
    from openhands.automation.event_schemas.bitbucket_data_center import (
        parse_bitbucket_data_center_event,
    )
    from openhands.automation.event_schemas.github import parse_github_event_auto
    from openhands.automation.event_schemas.jira_dc import parse_jira_dc_event

    # All three are forwarded by the OpenHands server, which signs with the
    # single shared AUTOMATION_WEBHOOK_SECRET into GitHub's header name.
    #
    # Only GitHub names its deliveries. Jira DC and Bitbucket DC send no
    # equivalent, so their events are recorded without being deduplicated --
    # which is the same position every custom webhook is in.
    for source, parse, event_id_header in (
        ("bitbucket_data_center", parse_bitbucket_data_center_event, None),
        ("github", parse_github_event_auto, "X-GitHub-Delivery"),
        ("jira_dc", parse_jira_dc_event, None),
    ):
        register_provider(
            Provider(
                source=source,
                parse=parse,
                secret_from_settings=lambda s: s.webhook_secret or None,
                event_id_header=event_id_header,
            )
        )


_register_builtin_providers()
