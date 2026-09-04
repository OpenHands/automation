"""
Webhook utility functions for event processing.

Contains helpers for webhook configuration lookup and automation run creation
for event-triggered automations. Signature verification and the description of
a source both live in `openhands.automation.providers`.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import get_settings
from openhands.automation.db import using_sqlite
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    CustomWebhook,
)
from openhands.automation.providers import (
    DEFAULT_VERIFIER,
    get_provider,
    is_builtin_source,
    verify_signature,
)
from openhands.automation.schemas import EventTrigger, WebhookConfig


logger = logging.getLogger("automation.utils.webhook")


# `is_builtin_source` and `verify_signature` now live in `providers`; re-exported
# here for callers that predate the registry.
__all__ = [
    "create_automation_run",
    "get_event_automations",
    "get_requested_event_types",
    "get_webhook_config",
    "is_builtin_source",
    "verify_signature",
]


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
    provider = get_provider(source)
    if provider is not None:
        secret = (
            provider.secret_from_settings(settings)
            if provider.secret_from_settings
            else None
        )
        if secret:
            return WebhookConfig(
                secret=secret,
                is_builtin=True,
                signature_header=provider.signature_header,
                signature_scheme=provider.verifier,
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
            # A cleared column reads as the default.
            signature_scheme=webhook.signature_scheme or DEFAULT_VERIFIER,
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

    # Ordered so an event matching several automations locks their runs in the
    # same order in every worker; otherwise two events on one subject deadlock.
    result = await session.execute(
        select(Automation).where(*base_filters, trigger_filter).order_by(Automation.id)
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


def _event_type_from_pattern(pattern: str) -> str:
    return pattern.split(".", 1)[0].strip()


async def get_requested_event_types(
    source: str,
    session: AsyncSession,
    *,
    supported_event_types: set[str] | None = None,
) -> list[str]:
    """Return source event types requested by enabled event automations.

    Trigger patterns can include actions (``pull_request.opened``) or wildcards
    (``pull_request.*``). The forwarding service only needs the top-level source
    event type. Unsupported requested types are omitted when a supported set is
    provided.
    """
    from sqlalchemy import func, literal

    base_filters = [
        Automation.enabled == True,  # noqa: E712
        Automation.deleted_at.is_(None),
    ]

    if using_sqlite():
        trigger_filter = and_(
            func.json_extract(Automation.trigger, "$.type") == literal("event"),
            func.json_extract(Automation.trigger, "$.source") == literal(source),
        )
    else:
        trigger_filter = and_(
            Automation.trigger.op("->>")("type") == literal("event"),
            Automation.trigger.op("->>")("source") == literal(source),
        )

    result = await session.execute(
        select(Automation.trigger).where(*base_filters, trigger_filter)
    )

    supported = (
        set(supported_event_types) if supported_event_types is not None else None
    )
    requested: set[str] = set()
    for trigger_data in result.scalars().all():
        try:
            trigger = EventTrigger.model_validate(trigger_data)
        except Exception as e:
            logger.warning("Failed to parse event trigger for requested types: %s", e)
            continue

        for pattern in trigger.event_patterns:
            event_type = _event_type_from_pattern(pattern)
            if not event_type:
                continue
            if event_type == "*":
                return sorted(supported) if supported is not None else ["*"]
            if supported is None or event_type in supported:
                requested.add(event_type)

    return sorted(requested)


async def create_automation_run(
    automation: Automation,
    session: AsyncSession,
    event_payload: dict[str, Any] | None = None,
    subject_key: str | None = None,
) -> AutomationRun:
    """
    Create a PENDING automation run for an event-triggered automation.

    Args:
        automation: The automation to run
        session: Database session
        event_payload: The webhook payload that triggered this run (optional)
                       For GitHub events: model_dump() of parsed Pydantic event
                       For custom webhooks: the raw payload dict
        subject_key: The external subject this run is about, for
                     `continue_conversation` triggers.

    Returns:
        The created AutomationRun instance
    """
    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=AutomationRunStatus.PENDING,
        event_payload=event_payload,
        telemetry_distinct_id=automation.telemetry_distinct_id,
        subject_key=subject_key,
    )
    session.add(run)
    return run
