"""
Event router for receiving webhook events and triggering automations.

Endpoint: POST /v1/events/{org_id}/{source}

Built-in sources (github, gitlab) verify signatures using the shared secret
from the OpenHands server. Custom sources verify using per-org webhook secrets.
"""

import hashlib
import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import get_settings
from automation.db import get_session
from automation.event_schemas import WebhookEvent, parse_event
from automation.models import Automation, AutomationRun, CustomWebhook
from automation.schemas import EventTrigger
from automation.trigger_matcher import matches_trigger

logger = logging.getLogger("automation.event_router")

router = APIRouter(prefix="/v1/events", tags=["events"])


class EventResponse(BaseModel):
    """Response for event processing."""

    received: bool
    matched: int
    runs_created: list[str]  # List of run IDs created


# --- Signature Verification ---


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify HMAC-SHA256 signature.

    Args:
        payload: Raw request body bytes
        signature: Signature from header (format: 'sha256=<hex>')
        secret: The shared secret

    Returns:
        True if signature is valid
    """
    if not signature.startswith("sha256="):
        return False

    expected_sig = signature[7:]  # Remove 'sha256=' prefix
    computed = hmac.new(
        secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, expected_sig)


async def _get_webhook_secret(
    source: str,
    org_id: uuid.UUID,
    session: AsyncSession,
) -> str | None:
    """
    Get the webhook secret for verifying signatures.

    For built-in sources (github), uses GITHUB_APP_WEBHOOK_SECRET.
    For custom sources, looks up the secret in the custom_webhooks table.
    """
    settings = get_settings()

    if source == "github":
        # Built-in GitHub integration uses the shared webhook secret
        return getattr(settings, "github_app_webhook_secret", None)
    elif source == "gitlab":
        # Built-in GitLab integration
        return getattr(settings, "gitlab_webhook_secret", None)
    else:
        # Custom webhook - look up in database
        result = await session.execute(
            select(CustomWebhook).where(
                CustomWebhook.org_id == org_id,
                CustomWebhook.source == source,
                CustomWebhook.enabled == True,  # noqa: E712
            )
        )
        webhook = result.scalar_one_or_none()
        if webhook:
            return webhook.webhook_secret
        return None


# --- Event Matching ---


async def _get_event_automations(
    org_id: uuid.UUID,
    source: str,
    session: AsyncSession,
) -> list[tuple[Automation, EventTrigger]]:
    """
    Get all enabled event-triggered automations for an org and source.

    Note: We query by source only. The actual event/action matching is done
    in-memory using the payload's matches() method, which supports wildcards.

    Args:
        org_id: The organization ID
        source: Event source (e.g., "github")
        session: Database session

    Returns:
        List of (Automation, EventTrigger) tuples
    """
    # Query for enabled automations with event triggers for this source
    # We can't filter by event pattern in DB because triggers support wildcards
    result = await session.execute(
        select(Automation).where(
            Automation.org_id == org_id,
            Automation.enabled == True,  # noqa: E712
            Automation.deleted_at.is_(None),
            Automation.trigger.contains({
                "type": "event",
                "source": source,
            }),
        )
    )
    automations = result.scalars().all()

    # Parse triggers and return pairs
    result_pairs: list[tuple[Automation, EventTrigger]] = []
    for automation in automations:
        try:
            trigger = EventTrigger.model_validate(automation.trigger)
            result_pairs.append((automation, trigger))
        except Exception as e:
            logger.warning(
                "Failed to parse trigger for automation %s: %s",
                automation.id,
                e,
            )

    return result_pairs


async def _create_automation_run(
    automation: Automation,
    event_payload: dict[str, Any],
    session: AsyncSession,
) -> AutomationRun:
    """Create a PENDING automation run for an event-triggered automation."""
    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        event_payload=event_payload,
    )
    session.add(run)
    return run


# --- Route Handler ---


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

    # 2. Get webhook secret for this source/org
    secret = await _get_webhook_secret(source, org_id, session)

    if not secret:
        logger.warning(
            "No webhook secret configured for source=%s org_id=%s",
            source,
            org_id,
        )
        raise HTTPException(
            status_code=404,
            detail=f"Unknown source or org: {source}",
        )

    # 3. Verify signature
    signature = x_hub_signature_256 or ""
    if not _verify_signature(body, signature, secret):
        logger.warning(
            "Invalid signature for event from source=%s org_id=%s",
            source,
            org_id,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 4. Parse payload
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("Failed to parse event payload: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # 5. Parse the event payload
    # For GitHub: OpenHands server sends {event_type, action, raw_payload, ...}
    # For custom: payload is the raw webhook
    event_type = payload.get("event_type") or payload.get("type") or "unknown"
    raw_payload = payload.get("raw_payload", payload)

    try:
        event: WebhookEvent = parse_event(source, event_type, raw_payload)
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
    # Query by source, then filter in-memory using event.matches()
    automations = await _get_event_automations(org_id, source, session)
    matched_automations = []

    for automation, trigger in automations:
        if matches_trigger(trigger, event):
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
        run = await _create_automation_run(
            automation,
            event_payload=payload,  # Store original payload
            session=session,
        )
        run_ids.append(str(run.id))

    await session.commit()

    return EventResponse(
        received=True,
        matched=len(matched_automations),
        runs_created=run_ids,
    )
