"""Provider descriptors and signature verifiers.

`PROVIDERS` describes an event provider in one place: how its payloads parse,
how a delivery is verified, where its secret comes from, what its transport
tolerates. `VERIFIERS` resolves a signature scheme by name, so a provider or a
per-org custom webhook can pick one. No transport imports.
"""

import base64
import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from openhands.automation.config import Settings
from openhands.automation.event_schemas import WebhookEvent
from openhands.automation.subjects import EventSubject, github_subject


if TYPE_CHECKING:
    from fastapi import Request, Response


DEFAULT_VERIFIER: Final[str] = "hmac_sha256_hex"
DEFAULT_BUILTIN_SIGNATURE_HEADER: Final[str] = "X-Hub-Signature-256"

STANDARD_WEBHOOKS_TOLERANCE_SECONDS: Final[int] = 300
STANDARD_WEBHOOKS_ID_HEADER: Final[str] = "webhook-id"
STANDARD_WEBHOOKS_TIMESTAMP_HEADER: Final[str] = "webhook-timestamp"

SLACK_TOLERANCE_SECONDS: Final[int] = 300
SLACK_TIMESTAMP_HEADER: Final[str] = "X-Slack-Request-Timestamp"


ParseFunc = Callable[[dict[str, Any]], WebhookEvent]
SecretFunc = Callable[[Settings], str | None]
SubjectFunc = Callable[[dict[str, Any]], EventSubject | None]
HandshakeFunc = Callable[["Request"], "Response | None"]


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
    # Header naming the provider's own delivery id, used to drop redeliveries.
    # None means the provider does not identify deliveries, so its events are
    # recorded but never deduplicated.
    event_id_header: str | None = None
    # Names the external thing an event is about, for a
    # `continue_conversation` trigger. None means such a trigger must supply
    # its own `subject_key_expr`.
    subject: SubjectFunc | None = None
    # Reserved. Deliberately not wired: no provider on the roadmap performs an
    # HTTP handshake, and Socket Mode is our Slack path.
    handshake: HandshakeFunc | None = None
    capabilities: Capabilities = Capabilities()


# Both registries are filled at the bottom of this module, once the verifier
# implementations and the parsers they name exist.
VERIFIERS: dict[str, WebhookVerifier] = {}
PROVIDERS: dict[str, Provider] = {}


def get_verifier(scheme: str | None) -> WebhookVerifier | None:
    """Resolve a signature scheme name, treating None as the default."""
    return VERIFIERS.get(scheme or DEFAULT_VERIFIER)


def verifier_schemes() -> frozenset[str]:
    """Every signature scheme a webhook may be configured with."""
    return frozenset(VERIFIERS)


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


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Look up a header case-insensitively, for callers passing a plain dict."""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lowered), None)


def _now_seconds() -> int:
    return int(time.time())


def _within_tolerance(timestamp: str, clock: Callable[[], int], tolerance: int) -> bool:
    """Whether a signed timestamp sits inside the replay window."""
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    return abs(clock() - sent_at) <= tolerance


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify a hex HMAC-SHA256 digest, with or without a `sha256=` prefix."""
    if signature.startswith("sha256="):
        signature = signature[7:]

    computed = hmac.new(
        secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Compared as bytes: a header carrying non-ASCII decodes to a str that
    # `compare_digest` refuses outright, raising instead of returning False.
    return hmac.compare_digest(computed.encode("utf-8"), signature.encode("utf-8"))


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
        # binascii.Error, which b64decode raises, subclasses ValueError.
        return base64.b64decode(key_material, validate=True)
    except ValueError:
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
        if not _within_tolerance(timestamp, self.clock, self.tolerance_seconds):
            return False

        key = standard_webhooks_key(secret)
        signed_content = f"{msg_id}.{timestamp}.".encode() + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        )

        # Space-separated list so a secret can be rotated without a gap; any one
        # match is enough, and every token is compared to keep the work
        # independent of where the match sits.
        matched = False
        for token in signature.split():
            version, _, candidate = token.partition(",")
            if not candidate or version != "v1":
                continue
            if hmac.compare_digest(candidate.encode("utf-8"), expected):
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
        if not _within_tolerance(timestamp, self.clock, self.tolerance_seconds):
            return False

        signed_content = b"v0:" + timestamp.encode("utf-8") + b":" + body
        expected = b"v0=" + hmac.new(
            secret.encode("utf-8"), signed_content, hashlib.sha256
        ).hexdigest().encode("utf-8")
        return hmac.compare_digest(signature.encode("utf-8"), expected)


VERIFIERS.update(
    {
        "hmac_sha256_hex": HmacSha256HexVerifier(),
        "standard_webhooks": StandardWebhooksVerifier(),
        "slack_v0": SlackV0Verifier(),
    }
)


def _register_builtin_providers() -> None:
    """Register the built-in providers. Called at module load."""
    from openhands.automation.event_schemas.bitbucket_data_center import (
        parse_bitbucket_data_center_event,
    )
    from openhands.automation.event_schemas.github import parse_github_event_auto
    from openhands.automation.event_schemas.jira_dc import parse_jira_dc_event

    # All forwarded by the OpenHands server, which signs with the single shared
    # AUTOMATION_WEBHOOK_SECRET into GitHub's header name. Only GitHub names its
    # deliveries; the other two are recorded without being deduplicated. Only
    # GitHub has a subject extractor; the others use `subject_key_expr`.
    for source, parse, event_id_header, subject in (
        ("bitbucket_data_center", parse_bitbucket_data_center_event, None, None),
        ("github", parse_github_event_auto, "X-GitHub-Delivery", github_subject),
        ("jira_dc", parse_jira_dc_event, None, None),
    ):
        register_provider(
            Provider(
                source=source,
                parse=parse,
                secret_from_settings=lambda s: s.webhook_secret or None,
                event_id_header=event_id_header,
                subject=subject,
            )
        )


_register_builtin_providers()
