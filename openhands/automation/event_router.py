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
    - `standard_webhooks` and `slack_v0` sign a timestamp and reject
      deliveries outside a 5-minute window, so they are not replayable
    - Header-based delivery IDs may not themselves be signed; a configured
      body-derived ID inherits verification of the raw body
    - Teams verifies the body but does not check timestamp freshness. Its
      replay protection lasts only while the event deduplication row is retained
"""

import json
import logging
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.acknowledgements import (
    TEAMS_ACKNOWLEDGEMENT,
    get_acknowledgement,
)
from openhands.automation.db import get_session
from openhands.automation.event_schemas import WebhookEvent, parse_event
from openhands.automation.event_schemas.catalog import (
    event_detection_rules,
    supported_event_types,
)
from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
)
from openhands.automation.ingest import (
    AcceptedEvent,
    ContinuationNotAllowed,
    accept_event,
)
from openhands.automation.observability import span
from openhands.automation.providers import (
    WebhookVerifier,
    get_header,
    get_verifier,
)
from openhands.automation.schemas import (
    EventDetectionRule,
    EventResponse,
    RequestedEventTypesResponse,
    TeamsMessageResponse,
    WebhookConfig,
)
from openhands.automation.utils.webhook import (
    get_event_automation_org_ids,
    get_requested_event_types,
    get_webhook_config,
)
from openhands.automation.webhook_policy import validate_webhook_protocol


logger = logging.getLogger("automation.event_router")


def _log_rejection(source: str, org_id: uuid.UUID, reason: str) -> None:
    """Log static reason codes, never delivery content or evaluation errors."""
    logger.warning(
        "Webhook rejected: reason=%s source=%s org=%s", reason, source, org_id
    )


router = APIRouter(prefix="/v1/events", tags=["events"])

# Sources whose forwarder cannot tell an event's organization from the event
# itself, so it delivers to every org with an automation on the source.
FAN_OUT_SOURCES = frozenset({"jira_dc"})


def _resolve_verifier(config: WebhookConfig, source: str) -> WebhookVerifier:
    """Resolve the configured scheme, refusing one this build cannot verify.

    Falling back would reject every genuine delivery as a bad signature, hiding
    a misconfiguration behind a 401.
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
    session: AsyncSession = Depends(get_session, scope="function"),
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
    # Signed content is the source string: this is a read, so there is no body.
    verifier = _resolve_verifier(config, source)
    if not verifier.verify(
        body=source.encode("utf-8"),
        headers=request.headers,
        secret=config.secret,
        signature_header=config.signature_header,
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")

    catalog = supported_event_types(source)
    if catalog is not None:
        # Forwarding of a cataloged source is prefiltered at the event-type
        # level. Return every type automation can parse so forwarding works
        # before any org has configured automations; automation performs the
        # granular action/filter matching after receiving the event.
        event_types = catalog
    else:
        event_types = await get_requested_event_types(source, session)
    return RequestedEventTypesResponse(
        source=source,
        event_types=event_types,
        event_detection_rules=[
            EventDetectionRule.model_validate(rule)
            for rule in event_detection_rules(source)
        ],
        org_ids=(
            await get_event_automation_org_ids(source, session)
            if source in FAN_OUT_SOURCES
            else None
        ),
    )


@router.post("/{org_id}/{source}", response_model=EventResponse | TeamsMessageResponse)
async def receive_event(
    org_id: uuid.UUID,
    source: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> EventResponse | Response:
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

    profile = get_acknowledgement(config.acknowledgement)
    if profile is None:
        raise HTTPException(500, "Unsupported webhook acknowledgement profile")
    teams = config.acknowledgement == TEAMS_ACKNOWLEDGEMENT
    if not config.is_builtin:
        try:
            validate_webhook_protocol(
                signature_scheme=config.signature_scheme,
                signature_header=config.signature_header,
                acknowledgement=config.acknowledgement,
                event_id_header=config.event_id_header,
                event_id_expr=config.event_id_expr,
                admission_expr=config.admission_expr,
                webhook_secret=config.secret,
            )
        except ValueError:
            raise HTTPException(500, "Invalid webhook protocol configuration") from None

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
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        if teams:
            _log_rejection(source, org_id, "invalid_json")
            raise HTTPException(400, "Invalid JSON") from None
        logger.warning("Malformed JSON in event payload: %s", e)
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if teams and (not isinstance(payload, dict) or payload.get("type") != "message"):
        _log_rejection(source, org_id, "unrecognized_activity")
        raise HTTPException(400, "Unrecognized Teams message activity")

    # Extract IDs only from authenticated bytes, never silently dropping an
    # invalid body ID and accepting without deduplication.
    provider_event_id = (
        get_header(request.headers, config.event_id_header)
        if config.event_id_header
        else None
    )
    if config.event_id_expr is not None:
        try:
            provider_event_id = evaluate_expression(config.event_id_expr, payload)
        except FilterEvaluationError:
            _log_rejection(source, org_id, "body_id_evaluation_failed")
            raise HTTPException(400, "Invalid body delivery ID") from None
        if (
            not isinstance(provider_event_id, str)
            or not provider_event_id.strip()
            or len(provider_event_id) > 255
        ):
            _log_rejection(source, org_id, "invalid_body_id")
            raise HTTPException(400, "Invalid body delivery ID")

    if config.admission_expr is not None:
        try:
            admitted = evaluate_expression(config.admission_expr, payload) is True
        except FilterEvaluationError:
            admitted = False
        if not admitted:
            _log_rejection(source, org_id, "admission_denied")
            raise HTTPException(
                403, "Webhook source admission policy rejected delivery"
            )

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
        if teams:
            _log_rejection(source, org_id, "unrecognized_activity")
            raise HTTPException(400, "Unrecognized Teams message activity") from None
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
        return EventResponse(received=True, matched=0, runs_created=[])
    except Exception as e:
        if teams:
            _log_rejection(source, org_id, "invalid_activity")
            raise HTTPException(400, "Invalid Teams message activity") from None
        logger.warning("Failed to parse event: %s", e)
        raise HTTPException(status_code=400, detail=f"Failed to parse event: {e}")

    logger.info(
        "Received %s event: key=%s org=%s",
        source,
        event.event_key,
        org_id,
    )

    # 6. Record, match triggers and create runs (transport-neutral)
    #
    with span(
        "automation.webhook.receive",
        {
            "automation.org_id": str(org_id),
            "automation.event.source": source,
            "automation.event.key": event.event_key,
            "automation.event.provider_event_id": provider_event_id,
        },
    ):
        try:
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
                allow_conversation_continuation=profile.deadline_seconds is None,
                defer_telemetry=profile.deadline_seconds is not None,
            )
        except ContinuationNotAllowed:
            raise HTTPException(
                409, "Deadline-bound sources cannot continue conversations"
            ) from None

    metadata = EventResponse(
        received=True,
        matched=result.matched,
        runs_created=result.run_ids,
        conversations_continued=result.conversation_ids,
    )
    if profile.deadline_seconds is not None:
        # FastAPI owns this best-effort work after the response body is sent
        # and the function-scoped database dependency has closed. Telemetry
        # therefore cannot hold intake locks or consume the protocol deadline.
        background_tasks.add_task(result.capture_telemetry, request=request)
    return profile.response(metadata.model_dump())
