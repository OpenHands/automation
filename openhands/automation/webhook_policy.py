"""Custom webhook configuration and deadline-bound routing constraints."""

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.acknowledgements import (
    DEFAULT_ACKNOWLEDGEMENT,
    TEAMS_ACKNOWLEDGEMENT,
    get_acknowledgement,
)
from openhands.automation.models import Automation, CustomWebhook
from openhands.automation.providers import TEAMS_VERIFIER, teams_webhook_key


CONTINUATION_CONFLICT = (
    "Deadline-bound webhook sources do not support continue_conversation triggers"
)


async def require_compatible_trigger(
    org_id: uuid.UUID, trigger: dict[str, Any], session: AsyncSession
) -> None:
    """Management API boundary for the transport-independent validator."""
    try:
        await validate_trigger_webhook_compatibility(org_id, trigger, session)
    except ValueError:
        raise HTTPException(422, CONTINUATION_CONFLICT) from None


def validate_webhook_protocol(
    *,
    signature_scheme: str | None,
    signature_header: str,
    acknowledgement: str | None,
    event_id_header: str | None,
    event_id_expr: str | None,
    admission_expr: str | None,
    webhook_secret: str | None,
) -> None:
    """Validate the merged configuration, without including secret input in errors."""
    name = acknowledgement or DEFAULT_ACKNOWLEDGEMENT
    if get_acknowledgement(name) is None:
        raise ValueError("Unknown webhook acknowledgement profile")
    if event_id_header is not None and event_id_expr is not None:
        raise ValueError("event_id_header and event_id_expr are mutually exclusive")
    teams = signature_scheme == TEAMS_VERIFIER
    if teams != (name == TEAMS_ACKNOWLEDGEMENT):
        raise ValueError(
            "The Teams verifier and acknowledgement must be selected together"
        )
    if teams:
        if signature_header.lower() != "authorization":
            raise ValueError("The Teams verifier requires the Authorization header")
        if not event_id_expr or not admission_expr:
            raise ValueError("Teams requires event_id_expr and admission_expr")
        teams_webhook_key(webhook_secret or "")


async def validate_trigger_webhook_compatibility(
    org_id: uuid.UUID, trigger: dict[str, Any], session: AsyncSession
) -> None:
    """Validate every automation producer, independently of event-key filters.

    EventTrigger currently has one exact source, not source patterns. Lists in
    `on` are event-key patterns and do not weaken this source-wide constraint.
    Disabled sources are included so enabling one cannot expose a conflict.
    """
    if (
        trigger.get("type") != "event"
        or trigger.get("destination") != "continue_conversation"
    ):
        return
    profiles = await session.scalars(
        select(CustomWebhook.acknowledgement).where(
            CustomWebhook.org_id == org_id,
            CustomWebhook.source == trigger.get("source"),
        )
    )
    if any(
        (profile := get_acknowledgement(name)) is not None
        and profile.deadline_seconds is not None
        for name in profiles
    ):
        raise ValueError(CONTINUATION_CONFLICT)


async def validate_source_webhook_compatibility(
    org_id: uuid.UUID,
    source: str,
    acknowledgement: str | None,
    session: AsyncSession,
) -> None:
    """Refuse a deadline profile if any non-deleted continuation uses the source.

    Include inactive/draft automations: activating them later must be safe too.
    Runtime ingestion independently checks its actual routing snapshot, so a
    concurrent configuration edit cannot introduce an agent-server call.
    """
    profile = get_acknowledgement(acknowledgement)
    if profile is None or profile.deadline_seconds is None:
        return
    triggers = await session.scalars(
        select(Automation.trigger).where(
            Automation.org_id == org_id, Automation.deleted_at.is_(None)
        )
    )
    if any(
        trigger.get("type") == "event"
        and trigger.get("source") == source
        and trigger.get("destination") == "continue_conversation"
        for trigger in triggers
    ):
        raise ValueError(CONTINUATION_CONFLICT)
