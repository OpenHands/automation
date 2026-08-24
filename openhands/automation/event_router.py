"""
Event router for receiving webhook events and triggering automations.

Endpoint: POST /v1/events/{org_id}/{source}

Built-in sources (github) verify signatures using the shared secret
from the OpenHands server. Custom sources verify using per-org webhook secrets.

Security Notes:
    - Rate limiting should be applied at the infrastructure layer (nginx/ALB)
      to prevent DoS attacks via HMAC verification spam
    - Recommended: limit by IP and by org_id
    - Request body size should be capped (e.g., 1MB) at the proxy level

TODO: Application-level rate limiting per org or org+source:
    - Track request counts in Redis with sliding window
    - Return 429 with Retry-After header when exceeded
    - Consider different limits for builtin (github) vs custom sources

Authentication Model:
    This endpoint uses HMAC signature verification instead of standard JWT auth.
    Webhooks are authenticated by verifying the signature against a shared secret.
    This is standard practice for webhook receivers (GitHub, Stripe, etc.).

    The scheme is per-source, resolved from `providers.VERIFIERS`. Built-in
    sources use hex HMAC-SHA256 over the raw body; a custom webhook picks one
    with its `signature_scheme`.

    Replay Attack Considerations:
    - Under `hmac_sha256_hex` old valid payloads could be replayed, since
      nothing in the signed content expires
    - The `standard_webhooks` and `slack_v0` schemes sign a timestamp and
      reject deliveries outside a 5-minute window, so they do not have this
      property
    - Events are deduplicated by the provider's delivery id, which is not
      itself signed: that stops a verbatim replay, not a crafted one
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import get_session
from openhands.automation.event_schemas import WebhookEvent, parse_event
from openhands.automation.event_schemas.github import (
    get_event_detection_rules,
    get_supported_event_types,
)
from openhands.automation.ingest import AcceptedEvent, accept_event
from openhands.automation.providers import (
    WebhookVerifier,
    get_header,
    get_provider,
    get_verifier,
)
from openhands.automation.schemas import (
    EventDetectionRule,
    EventResponse,
    RequestedEventTypesResponse,
    WebhookConfig,
)
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.utils.webhook import (
    get_requested_event_types,
    get_webhook_config,
)


logger = logging.getLogger("automation.event_router")

router = APIRouter(prefix="/v1/events", tags=["events"])


def _resolve_verifier(config: WebhookConfig, source: str) -> WebhookVerifier:
    """Resolve the configured signature scheme to a verifier.

    A scheme with no verifier behind it means the stored configuration names
    something this build does not implement. Refusing the delivery is the only
    safe reading: the alternative is falling back to a scheme the sender is not
    using, which would reject every genuine event as a bad signature and read
    as an authentication failure rather than the misconfiguration it is.
    """
    verifier = get_verifier(config.signature_scheme)
    if verifier is None:
        logger.error(
            "Unknown signature scheme '%s' configured for source=%s",
            config.signature_scheme,
            source,
        )
        raise HTTPException(
            status_code=500,
            detail="Webhook signature scheme is not supported by this deployment",
        )
    return verifier


@router.get("/{source}/requested-types", response_model=RequestedEventTypesResponse)
async def requested_event_types(
    source: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RequestedEventTypesResponse:
    """Return event types currently requested by enabled automations.

    This endpoint is intended for trusted webhook forwarders. Built-in sources
    authenticate with the same shared webhook secret used for event delivery.
    The signature is computed over the UTF-8 source string.
    """
    config = await get_webhook_config(source, uuid.UUID(int=0), session)
    if not config or not config.is_builtin:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown builtin webhook source: {source}",
        )

    signature = request.headers.get(config.signature_header)
    if not signature:
        raise HTTPException(
            status_code=401,
            detail=f"Missing signature header: {config.signature_header}",
        )
    # The signed content here is the source string, not a request body: this is
    # a read, so there is nothing else for the forwarder to authenticate with.
    verifier = _resolve_verifier(config, source)
    if not verifier.verify(
        body=source.encode("utf-8"),
        headers=request.headers,
        secret=config.secret,
        signature_header=config.signature_header,
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")

    if source == "github":
        # GitHub forwarding is prefiltered at the event-family level. Return all
        # event families automation can parse so webhook forwarding still works
        # before any org has configured automations; automation performs the
        # granular action/filter matching after receiving the event.
        event_types = get_supported_event_types()
    else:
        event_types = await get_requested_event_types(source, session)
    event_detection_rules = (
        [
            EventDetectionRule.model_validate(rule)
            for rule in get_event_detection_rules()
        ]
        if source == "github"
        else []
    )
    return RequestedEventTypesResponse(
        source=source,
        event_types=event_types,
        event_detection_rules=event_detection_rules,
    )


@router.post("/{org_id}/{source}", response_model=EventResponse)
async def receive_event(
    org_id: uuid.UUID,
    source: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> EventResponse:
    """
    Receive a webhook event from a source.

    For built-in sources (github), the event is forwarded from the
    OpenHands server with a normalized payload.

    For custom sources, the raw webhook payload is received directly.

    The payload signature is verified using:
    - AUTOMATION_WEBHOOK_SECRET for github (builtin, header: X-Hub-Signature-256)
    - Per-org webhook_secret for custom sources (header configured per webhook)
    """
    # 1. Read raw body for signature verification
    body = await request.body()

    # 2. Get webhook config for this source/org
    config = await get_webhook_config(source, org_id, session)

    if not config:
        logger.warning(
            "No webhook configured for source=%s org_id=%s",
            source,
            org_id,
        )
        raise HTTPException(
            status_code=404,
            detail=f"Unknown webhook source: {source}",
        )

    # 3. Get signature from the configured header (source-specific)
    signature = request.headers.get(config.signature_header)

    if not signature:
        logger.warning(
            "Missing signature header '%s' for event from source=%s org_id=%s",
            config.signature_header,
            source,
            org_id,
        )
        raise HTTPException(
            status_code=401,
            detail=f"Missing signature header: {config.signature_header}",
        )

    verifier = _resolve_verifier(config, source)
    if not verifier.verify(
        body=body,
        headers=request.headers,
        secret=config.secret,
        signature_header=config.signature_header,
    ):
        logger.warning(
            "Invalid signature (scheme=%s) for event from source=%s org_id=%s",
            config.signature_scheme,
            source,
            org_id,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 4. Parse JSON payload
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        logger.warning("Malformed JSON in event payload: %s", e)
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # 5. Parse the event into a typed WebhookEvent
    # webhook_payload is the actual webhook payload used for filter matching
    try:
        if config.is_builtin:
            # Built-in sources (github): extract nested payload, auto-detect event type
            if "payload" not in payload:
                raise HTTPException(
                    status_code=400,
                    detail="Missing payload in builtin source request",
                )
            webhook_payload = payload["payload"]
            event: WebhookEvent = parse_event(source, webhook_payload)
        else:
            # Custom webhooks: extract event_key using JMESPath expression
            webhook_payload = payload
            event = parse_event(
                source, webhook_payload, event_key_expr=config.event_key_expr
            )
    except HTTPException:
        raise  # Re-raise HTTPExceptions as-is
    except ValueError as e:
        # Unknown event type (e.g. a GitHub event we don't have a detection
        # rule for). This is normal - the service supports a subset of event
        # types from each source - so acknowledge the webhook with matched=0
        # rather than failing with 400. See APP-2668.
        logger.info(
            "Ignoring unrecognized event from source=%s org=%s: %s",
            source,
            org_id,
            e,
        )
        await capture_automation_event(
            "automation_event_ignored",
            request=request,
            properties={
                "event_source": source,
                "org_id": str(org_id),
                "ignore_reason": "unrecognized_event",
            },
        )
        return EventResponse(received=True, matched=0, runs_created=[])
    except Exception as e:
        logger.warning("Failed to parse event: %s", e)
        raise HTTPException(status_code=400, detail=f"Failed to parse event: {e}")

    logger.info(
        "Received %s event: key=%s org=%s",
        source,
        event.event_key,
        org_id,
    )
    await capture_automation_event(
        "automation_event_received",
        request=request,
        properties={
            "event_source": source,
            "event_key": event.event_key,
            "org_id": str(org_id),
            "webhook_builtin": config.is_builtin,
        },
    )

    # 6. Record, match triggers and create runs (transport-neutral)
    #
    # The delivery id is where HTTP has to do the looking: `accept_event()`
    # deduplicates on whatever the transport hands it, and for a webhook that
    # is a header. A provider that names no header, and every custom webhook,
    # yields None -- recorded, routed, not deduplicated.
    provider = get_provider(source)
    provider_event_id = (
        get_header(request.headers, provider.event_id_header)
        if provider is not None and provider.event_id_header
        else None
    )
    result = await accept_event(
        org_id,
        AcceptedEvent(
            source=source,
            event_key=event.event_key,
            payload=webhook_payload,
            provider_event_id=provider_event_id,
            parsed_event=event if isinstance(event, BaseModel) else None,
        ),
        session,
        request=request,
    )

    return EventResponse(
        received=True,
        matched=result.matched,
        runs_created=result.run_ids,
    )
