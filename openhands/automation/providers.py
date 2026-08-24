"""Provider descriptors and signature verifiers.

`PROVIDERS` describes an event provider in one place: how its payloads parse,
how a delivery is verified, where its secret comes from, what its transport
tolerates. `VERIFIERS` resolves a signature scheme by name, so a provider or a
per-org custom webhook can pick one. No transport imports.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from openhands.automation.config import Settings
from openhands.automation.event_schemas import WebhookEvent


if TYPE_CHECKING:
    from fastapi import Request, Response

    from openhands.automation.ingest import EventSubject


logger = logging.getLogger("automation.providers")


DEFAULT_VERIFIER: Final[str] = "hmac_sha256_hex"
DEFAULT_BUILTIN_SIGNATURE_HEADER: Final[str] = "X-Hub-Signature-256"

STANDARD_WEBHOOKS_TOLERANCE_SECONDS: Final[int] = 300
STANDARD_WEBHOOKS_ID_HEADER: Final[str] = "webhook-id"
STANDARD_WEBHOOKS_TIMESTAMP_HEADER: Final[str] = "webhook-timestamp"

SLACK_TOLERANCE_SECONDS: Final[int] = 300
SLACK_TIMESTAMP_HEADER: Final[str] = "X-Slack-Request-Timestamp"


ParseFunc = Callable[[dict[str, Any]], WebhookEvent]
SecretFunc = Callable[[Settings], str | None]
SubjectFunc = Callable[[dict[str, Any]], "EventSubject | None"]
HandshakeFunc = Callable[["Request"], "Response | None"]


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Look up a header case-insensitively, for callers passing a plain dict."""
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


class WebhookVerifier(Protocol):
    """Proves a raw delivery was signed by the holder of the shared secret.

    `signature_header` is a parameter because custom webhooks configure it per
    row; anything else a scheme needs is read from `headers`, since those names
    are fixed by the scheme.
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
    """Verify a hex HMAC-SHA256 digest, with or without a `sha256=` prefix."""
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


def standard_webhooks_key(secret: str) -> bytes:
    """Strip the `whsec_` prefix and base64-decode; fall back to raw bytes."""
    key_material = secret
    if key_material.startswith("whsec_"):
        key_material = key_material[len("whsec_") :]
    try:
        return base64.b64decode(key_material, validate=True)
    except (binascii.Error, ValueError):
        return secret.encode("utf-8")


@dataclass(frozen=True, slots=True)
class StandardWebhooksVerifier:
    """standardwebhooks.com: base64 HMAC over `{id}.{timestamp}.{body}`.

    The timestamp is signed, so the freshness window is real replay protection.
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

        # Space-separated list so a secret can be rotated without a gap; any one
        # match is enough, and every token is compared to keep the work
        # independent of where the match sits.
        matched = False
        for token in signature.split():
            version, _, candidate = token.partition(",")
            if not candidate or version != "v1":
                continue
            if hmac.compare_digest(candidate, expected):
                matched = True
        return matched


@dataclass(frozen=True, slots=True)
class SlackV0Verifier:
    """Slack Events API: hex HMAC over `v0:{timestamp}:{body}`."""

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


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a provider's transport tolerates. Declared, never assumed."""

    # Slack load-balances a payload across an app's open connections and
    # endorses up to 10, so Socket Mode is safe to run active-active; an
    # arbitrary WebSocket upstream would duplicate every event per replica.
    tolerates_multiple_connections: bool = False


@dataclass(frozen=True, slots=True)
class Provider:
    """Everything the service knows about one event provider."""

    source: str
    parse: ParseFunc
    verifier: str = DEFAULT_VERIFIER
    signature_header: str = DEFAULT_BUILTIN_SIGNATURE_HEADER
    secret_from_settings: SecretFunc | None = None
    # Reserved for subject-based routing. Nothing reads it.
    subject: SubjectFunc | None = None
    # Reserved. Deliberately not wired: no provider on the roadmap performs an
    # HTTP handshake, and Socket Mode is our Slack path.
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
    """Source names a custom webhook may not claim, derived from the registry."""
    return frozenset(PROVIDERS)


def _register_builtin_providers() -> None:
    """Register the built-in providers. Called at module load."""
    from openhands.automation.event_schemas.bitbucket_data_center import (
        parse_bitbucket_data_center_event,
    )
    from openhands.automation.event_schemas.github import parse_github_event_auto
    from openhands.automation.event_schemas.jira_dc import parse_jira_dc_event

    # All forwarded by the OpenHands server, which signs with the single shared
    # AUTOMATION_WEBHOOK_SECRET into GitHub's header name.
    for source, parse in (
        ("bitbucket_data_center", parse_bitbucket_data_center_event),
        ("github", parse_github_event_auto),
        ("jira_dc", parse_jira_dc_event),
    ):
        register_provider(
            Provider(
                source=source,
                parse=parse,
                secret_from_settings=lambda s: s.webhook_secret or None,
            )
        )


_register_builtin_providers()
