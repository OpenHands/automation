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

    Replay Attack Considerations:
    - Old valid payloads could be replayed since signatures don't expire
    - GitHub includes delivery IDs for deduplication; consider tracking these
    - For high-security scenarios, add timestamp validation (X-GitHub-Timestamp)
    - Current risk is acceptable: replay triggers same automation again (idempotent)
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import get_session
from openhands.automation.event_schemas import WebhookEvent, parse_event
from openhands.automation.schemas import EventResponse
from openhands.automation.trigger_matcher import matches_trigger
from openhands.automation.utils.webhook import (
    create_automation_run,
    get_event_automations,
    get_webhook_config,
    verify_signature,
    verify_standard_webhooks,
)


logger = logging.getLogger("automation.event_router")

router = APIRouter(prefix="/v1/events", tags=["events"])


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

    # 3. Verify the signature according to the configured scheme.
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

    if config.signature_scheme == "standard_webhooks":
        # Standard Webhooks (e.g. GitLab 19.1+ signing tokens): the signature is
        # a "v1,<base64>" value plus webhook-id / webhook-timestamp headers.
        valid = verify_standard_webhooks(
            body,
            config.secret,
            request.headers.get("webhook-id"),
            request.headers.get("webhook-timestamp"),
            signature,
        )
    else:
        # Default: GitHub/Linear style hex HMAC over the raw body.
        valid = verify_signature(body, signature, config.secret)

    if not valid:
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

    # 6. Find matching automations
    automations = await get_event_automations(org_id, source, session)
    matched_automations = []

    for automation, trigger in automations:
        # Match trigger against webhook payload using JMESPath filter
        if matches_trigger(trigger, source, event.event_key, webhook_payload):
            matched_automations.append(automation)

    logger.info(
        "Event matched %d/%d automations for org=%s",
        len(matched_automations),
        len(automations),
        org_id,
    )

    # 7. Create PENDING runs for matched automations
    # For Pydantic-parsed events (GitHub), use model_dump() for typed fields
    # For custom webhooks, use the webhook payload directly
    event_payload = (
        event.model_dump(mode="json")
        if isinstance(event, BaseModel)
        else webhook_payload
    )

    run_ids: list[str] = []
    for automation in matched_automations:
        run = await create_automation_run(
            automation, session, event_payload=event_payload
        )
        run_ids.append(str(run.id))

    await session.commit()

    return EventResponse(
        received=True,
        matched=len(matched_automations),
        runs_created=run_ids,
    )
