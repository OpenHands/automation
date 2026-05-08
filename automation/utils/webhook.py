"""
Webhook utility functions for event processing.

Contains helpers for signature verification, webhook configuration lookup,
and automation run creation for event-triggered automations.
"""

import hashlib
import hmac
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from automation.config import Settings, get_settings
from automation.db import using_sqlite
from automation.models import Automation, AutomationRun, CustomWebhook
from automation.schemas import EventTrigger, WebhookConfig


logger = logging.getLogger("automation.utils.webhook")


# =============================================================================
# Builtin Source Registry
# =============================================================================
# Registry pattern for builtin webhook sources. Each source maps to a function
# that extracts the webhook secret from settings. Add new integrations here.

BuiltinConfigFunc = Callable[[Settings], str | None]

BUILTIN_SOURCES: dict[str, BuiltinConfigFunc] = {
    "github": lambda s: s.webhook_secret or None,
    "gitlab": lambda s: s.webhook_secret or None,
}


def register_builtin_source(source: str, config_func: BuiltinConfigFunc) -> None:
    """Register a new builtin webhook source.

    Args:
        source: Source name (e.g., "bitbucket")
        config_func: Function that extracts the webhook secret from Settings
    """
    BUILTIN_SOURCES[source] = config_func


def is_builtin_source(source: str) -> bool:
    """Check if a source is a builtin integration."""
    return source in BUILTIN_SOURCES


# =============================================================================
# Webhook Functions
# =============================================================================


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


async def get_webhook_config(
    source: str,
    org_id: uuid.UUID,
    session: AsyncSession,
) -> WebhookConfig | None:
    """
    Get the webhook configuration for verifying signatures and parsing events.

    For built-in sources (github), uses settings from environment.
    For custom sources, looks up config in the custom_webhooks table.

    Args:
        source: Event source (e.g., "github", "stripe")
        org_id: Organization ID
        session: Database session

    Returns:
        WebhookConfig with secret and parsing settings, or None if not found.
    """
    settings = get_settings()

    # Check builtin sources first
    if source in BUILTIN_SOURCES:
        config_func = BUILTIN_SOURCES[source]
        secret = config_func(settings)
        if secret:
            return WebhookConfig(
                secret=secret,
                is_builtin=True,
                signature_header="X-Hub-Signature-256",  # GitHub's header
            )
        return None

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
            event_key_expr=webhook.event_key_expr,
            signature_header=webhook.signature_header,
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
    #
    # Database-specific handling for JSON column (generic JSON, not JSONB):
    # - PostgreSQL: Use ->> operator to extract text values from JSON
    # - SQLite: Use json_extract() function
    from sqlalchemy import func, literal

    base_filters = [
        Automation.org_id == org_id,
        Automation.enabled == True,  # noqa: E712
        Automation.deleted_at.is_(None),
    ]

    if using_sqlite():
        # SQLite: Use json_extract for type and source matching
        # json_extract returns the value at the path, or NULL if not found
        trigger_filter = and_(
            func.json_extract(Automation.trigger, "$.type") == literal("event"),
            func.json_extract(Automation.trigger, "$.source") == literal(source),
        )
    else:
        # PostgreSQL: Use ->> operator to extract text values from JSON
        # trigger->>'type' returns the text value of the 'type' key
        # Note: .astext only works with JSONB, use op('->>') for generic JSON
        trigger_filter = and_(
            Automation.trigger.op("->>")("type") == literal("event"),
            Automation.trigger.op("->>")("source") == literal(source),
        )

    result = await session.execute(
        select(Automation).where(*base_filters, trigger_filter)
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
    session: AsyncSession,
    event_payload: dict[str, Any] | None = None,
) -> AutomationRun:
    """
    Create a PENDING automation run for an event-triggered automation.

    Args:
        automation: The automation to run
        session: Database session
        event_payload: The webhook payload that triggered this run (optional)
                       For GitHub events: model_dump() of parsed Pydantic event
                       For custom webhooks: the raw payload dict

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
