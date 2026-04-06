"""
Webhook utility functions for event processing.

Contains helpers for signature verification, webhook configuration lookup,
and automation run creation for event-triggered automations.
"""

import hashlib
import hmac
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import get_settings
from automation.models import Automation, AutomationRun, CustomWebhook
from automation.schemas import EventTrigger, WebhookConfig


logger = logging.getLogger("automation.utils.webhook")


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
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


async def get_webhook_config(
    source: str,
    org_id: uuid.UUID,
    session: AsyncSession,
) -> WebhookConfig | None:
    """
    Get the webhook configuration for verifying signatures and parsing events.

    For built-in sources (github), uses GITHUB_APP_WEBHOOK_SECRET.
    For custom sources, looks up config in the custom_webhooks table.

    Args:
        source: Event source (e.g., "github", "stripe")
        org_id: Organization ID
        session: Database session

    Returns:
        WebhookConfig with secret and parsing settings, or None if not found.
    """
    settings = get_settings()

    if source == "github":
        secret = getattr(settings, "github_app_webhook_secret", None)
        if secret:
            return WebhookConfig(secret=secret, is_builtin=True)
        return None
    elif source == "gitlab":
        secret = getattr(settings, "gitlab_webhook_secret", None)
        if secret:
            return WebhookConfig(secret=secret, is_builtin=True)
        return None
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
            return WebhookConfig(
                secret=webhook.webhook_secret,
                is_builtin=False,
                event_type_paths=webhook.event_type_paths,
            )
        return None


async def get_event_automations(
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
            Automation.trigger.contains(
                {
                    "type": "event",
                    "source": source,
                }
            ),
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


async def create_automation_run(
    automation: Automation,
    event_payload: dict[str, Any],
    session: AsyncSession,
) -> AutomationRun:
    """
    Create a PENDING automation run for an event-triggered automation.

    Args:
        automation: The automation to run
        event_payload: The webhook payload that triggered this run
        session: Database session

    Returns:
        The created AutomationRun instance
    """
    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        event_payload=event_payload,
    )
    session.add(run)
    return run
