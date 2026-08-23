"""Transport-neutral event ingestion.

`accept_event()` separates how an event arrived from what the service does with
it. Transports own acquisition, authentication and interpretation; trigger
matching and run creation live here and are shared by every transport.
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


@dataclass(frozen=True, slots=True)
class EventSubject:
    """The external thing an event is about. Reserved; nothing reads it yet."""

    key: str


@dataclass(frozen=True, slots=True)
class AcceptedEvent:
    """An authenticated, interpreted event, ready to be routed."""

    source: str
    event_key: str
    # Raw provider payload; JMESPath trigger filters run on this.
    payload: dict[str, Any] = field(default_factory=dict)
    provider_event_id: str | None = None
    subject: EventSubject | None = None
    occurred_at: datetime | None = None
    # When set, persisted as the run's event_payload in place of `payload`.
    parsed_event: BaseModel | None = None


@dataclass(frozen=True, slots=True)
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
    """Route an already-authenticated event to the org's matching automations.

    Commits before returning. `request` is telemetry only; non-HTTP transports
    omit it and every telemetry event still fires.
    """
    source = event.source
    webhook_payload = event.payload

    automations = await get_event_automations(org_id, source, session)
    matched_automations = []

    for automation, trigger in automations:
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

    # Typed events (GitHub) keep their model shape; others store the payload.
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
