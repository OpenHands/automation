"""
Transport-neutral event ingestion.

`accept_event()` is the boundary between *how an event arrived* and *what the
service does with it*. Everything above the boundary — reading bytes, proving
the event is genuine, turning a provider payload into an ``event_key`` — belongs
to the transport. Everything below it — finding the org's event automations,
matching triggers, creating PENDING runs — is identical for every transport and
lives here.

Authentication is deliberately **not** part of this module. HTTP proves
authenticity with an HMAC over the raw body; a Socket Mode connection proves it
by possession of an app-level token over TLS; a poller proves it by having made
the outbound call itself. `accept_event()` receives an already-authenticated
event and never asks how it was authenticated. Without that rule, every
non-HTTP transport is tempted to manufacture a signature to satisfy a check that
does not apply to it.

The only caller today is the webhook handler in `event_router.py`.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.telemetry import capture_automation_event
from openhands.automation.trigger_matcher import matches_trigger
from openhands.automation.utils.webhook import (
    create_automation_run,
    get_event_automations,
)


logger = logging.getLogger("automation.ingest")


@dataclass
class EventSubject:
    """The external thing an event is about (a Slack thread, a pull request).

    Reserved for subject -> conversation routing; nothing populates or reads it
    yet. `key` is the value that will be stored as `subject_key`.
    """

    key: str


@dataclass
class AcceptedEvent:
    """An authenticated, interpreted event, ready to be routed.

    The transport owns everything that produces one of these: acquiring the
    bytes, proving they are genuine, and deriving `event_key`.
    """

    source: str
    event_key: str
    # Raw provider payload. JMESPath trigger filters run against this.
    payload: dict[str, Any] = field(default_factory=dict)
    provider_event_id: str | None = None
    subject: EventSubject | None = None
    occurred_at: datetime | None = None
    # Optional typed event, when the transport parsed the payload into a
    # Pydantic model (the webhook handler does this via `parse_event()`). When
    # present it — not `payload` — is what gets persisted as the run's
    # `event_payload`, preserving the shape existing automations already see.
    parsed_event: BaseModel | None = None


@dataclass
class AcceptResult:
    """The outcome of routing an accepted event."""

    matched: int
    run_ids: list[str]
    duplicate: bool = False


async def accept_event(
    org_id: uuid.UUID,
    event: AcceptedEvent,
    session: AsyncSession,
    *,
    request: Request | None = None,
) -> AcceptResult:
    """
    Route an already-authenticated event to the org's matching automations.

    Args:
        org_id: The organization the event belongs to
        event: The authenticated, interpreted event
        session: Database session (committed before returning)
        request: Originating HTTP request, for telemetry only. Non-HTTP
                 transports omit it; every telemetry event still fires.

    Returns:
        AcceptResult with the number of matched automations and the IDs of the
        runs created for them.
    """
    source = event.source
    webhook_payload = event.payload

    # 1. Find matching automations
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
    await capture_automation_event(
        "automation_event_matched",
        request=request,
        properties={
            "event_source": source,
            "event_key": event.event_key,
            "org_id": str(org_id),
            "candidate_count": len(automations),
            "matched_count": len(matched_automations),
        },
    )

    # 2. Create PENDING runs for matched automations
    # For Pydantic-parsed events (GitHub), use model_dump() for typed fields
    # For custom webhooks, use the webhook payload directly
    event_payload = (
        event.parsed_event.model_dump(mode="json")
        if isinstance(event.parsed_event, BaseModel)
        else webhook_payload
    )

    run_ids: list[str] = []
    for automation in matched_automations:
        run = await create_automation_run(
            automation, session, event_payload=event_payload
        )
        run_ids.append(str(run.id))
        run_properties = {
            "trigger_source": "event",
            "event_source": source,
            "event_key": event.event_key,
        }
        await capture_automation_event(
            "automation_run_scheduled",
            request=request,
            automation=automation,
            run=run,
            properties=run_properties,
        )
        await capture_automation_event(
            "automation_run_created",
            request=request,
            automation=automation,
            run=run,
            properties=run_properties,
        )

    await session.commit()

    return AcceptResult(
        matched=len(matched_automations),
        run_ids=run_ids,
    )
