"""Configured event source discovery across delivery transports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import StreamSettings, get_config
from openhands.automation.models import CustomWebhook
from openhands.automation.providers import builtin_sources
from openhands.automation.streams.supervisor import build_stream_providers
from openhands.automation.utils.webhook import get_webhook_config


_STREAM_EVENT_KEY_EXPRS = {
    "slack": "event.type",
}


@dataclass(frozen=True, slots=True)
class EventSourceDelivery:
    """How a configured event source can derive an event key."""

    source: str
    event_key_expr: str | None = None
    transport: str = "webhook"


async def get_event_source_delivery(
    source: str,
    org_id: uuid.UUID,
    session: AsyncSession,
) -> EventSourceDelivery | None:
    """Return configured delivery metadata for a source, if it can emit events."""
    webhook = await get_webhook_config(source, org_id, session)
    if webhook is not None:
        return EventSourceDelivery(
            source=source,
            event_key_expr=webhook.event_key_expr,
            transport="webhook",
        )

    if source in configured_stream_sources(org_id):
        return EventSourceDelivery(
            source=source,
            event_key_expr=_STREAM_EVENT_KEY_EXPRS.get(source),
            transport="stream",
        )

    return None


async def is_event_source_configured(
    source: str,
    org_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    """Whether a source can deliver events to this organization."""
    return await get_event_source_delivery(source, org_id, session) is not None


async def configured_event_sources(
    org_id: uuid.UUID,
    session: AsyncSession,
) -> list[str]:
    """All configured event source names for an organization."""
    config = get_config()
    builtin = builtin_sources() if config.service.webhook_secret else []
    return sorted(
        {
            *builtin,
            *await custom_event_sources(org_id, session),
            *configured_stream_sources(org_id, config.streams),
        }
    )


async def custom_event_sources(
    org_id: uuid.UUID,
    session: AsyncSession,
) -> list[str]:
    """Enabled custom webhook source names for an organization."""
    result = await session.execute(
        select(CustomWebhook.source)
        .where(CustomWebhook.org_id == org_id, CustomWebhook.enabled.is_(True))
        .distinct()
    )
    return list(result.scalars().all())


def configured_stream_sources(
    org_id: uuid.UUID,
    settings: StreamSettings | None = None,
) -> list[str]:
    """Stream source names configured and enabled for an organization."""
    settings = settings if settings is not None else get_config().streams
    if not settings.enabled:
        return []
    return sorted(
        {
            provider.source
            for provider in build_stream_providers(settings)
            if provider.org_id == org_id
        }
    )
