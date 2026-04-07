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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from automation.db import get_session
from automation.event_schemas import WebhookEvent, parse_event
from automation.schemas import EventResponse
from automation.trigger_matcher import matches_trigger
from automation.utils.webhook import (
    create_automation_run,
    get_event_automations,
    get_webhook_config,
    verify_signature,
)


logger = logging.getLogger("automation.event_router")

router = APIRouter(prefix="/v1/events", tags=["events"])


@router.post("/{org_id}/{source}", response_model=EventResponse)
async def receive_event(
    org_id: uuid.UUID,
    source: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> EventResponse:
    """
    Receive a webhook event from a source.

    For built-in sources (github), the event is forwarded from the
    OpenHands server with a normalized payload.

    For custom sources, the raw webhook payload is received directly.

    The payload signature is verified using:
    - AUTOMATION_WEBHOOK_SECRET for github (builtin)
    - Per-org webhook_secret from custom_webhooks table for custom sources
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

    # 3. Verify signature
    if not x_hub_signature_256:
        logger.warning(
            "Missing signature for event from source=%s org_id=%s",
            source,
            org_id,
        )
        raise HTTPException(status_code=401, detail="Missing signature")

    if not verify_signature(body, x_hub_signature_256, config.secret):
        logger.warning(
            "Invalid signature for event from source=%s org_id=%s",
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
    # raw_payload is the payload used for filter matching
    try:
        if config.is_builtin:
            # Built-in sources (github): extract raw_payload, auto-detect event type
            if "raw_payload" not in payload:
                raise HTTPException(
                    status_code=400,
                    detail="Missing raw_payload in builtin source payload",
                )
            raw_payload = payload["raw_payload"]
            event: WebhookEvent = parse_event(source, raw_payload)
        else:
            # Custom webhooks: extract event_key using JMESPath expression
            raw_payload = payload
            event = parse_event(
                source, raw_payload, event_key_expr=config.event_key_expr
            )
    except HTTPException:
        raise  # Re-raise HTTPExceptions as-is
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
        # Match trigger against raw payload using JMESPath filter
        if matches_trigger(trigger, source, event.event_key, raw_payload):
            matched_automations.append(automation)

    logger.info(
        "Event matched %d/%d automations for org=%s",
        len(matched_automations),
        len(automations),
        org_id,
    )

    # 7. Create PENDING runs for matched automations
    # For Pydantic-parsed events (GitHub), use model_dump() for typed fields
    # For custom webhooks, use the raw payload directly
    event_payload = (
        event.model_dump(mode="json") if hasattr(event, "model_dump") else raw_payload
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
