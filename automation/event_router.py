"""
Event router for receiving webhook events and triggering automations.

Endpoint: POST /v1/events/{org_id}/{source}

Built-in sources (github, gitlab) verify signatures using the shared secret
from the OpenHands server. Custom sources verify using per-org webhook secrets.

Security Notes:
    - Rate limiting should be applied at the infrastructure layer (nginx/ALB)
      to prevent DoS attacks via HMAC verification spam
    - Recommended: limit by IP and by org_id
    - Request body size should be capped (e.g., 1MB) at the proxy level
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

    For built-in sources (github, gitlab), the event is forwarded from the
    OpenHands server with a normalized payload.

    For custom sources, the raw webhook payload is received directly.

    The payload signature is verified using:
    - GITHUB_APP_WEBHOOK_SECRET for github
    - GITLAB_WEBHOOK_SECRET for gitlab
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
            # Built-in sources (github): event_type comes from preprocessed payload
            event_type = payload.get("event_type")
            if not event_type:
                raise HTTPException(
                    status_code=400,
                    detail="Missing event_type in builtin source payload",
                )
            if "raw_payload" not in payload:
                raise HTTPException(
                    status_code=400,
                    detail="Missing raw_payload in builtin source payload",
                )
            raw_payload = payload["raw_payload"]
            event: WebhookEvent = parse_event(
                source, raw_payload, event_type=event_type
            )
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

    # 7. Create runs for matched automations
    run_ids: list[str] = []
    for automation in matched_automations:
        run = await create_automation_run(automation, session)
        run_ids.append(str(run.id))

    await session.commit()

    return EventResponse(
        received=True,
        matched=len(matched_automations),
        runs_created=run_ids,
    )
