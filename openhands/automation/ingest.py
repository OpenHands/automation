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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.models import IntegrationEvent
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
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AcceptResult:
    """Route an already-authenticated event to the org's matching automations.

    Records the event and creates the runs in one transaction, deduplicating on
    `event.provider_event_id` when the transport supplies one. Commits before
    returning.

    `request` and `session_factory` are both telemetry plumbing. Telemetry
    resolves its distinct id from the database, and HTTP callers supply that
    reader indirectly as `request.app.state.session_factory`; a caller that
    passes neither drops every event silently. So non-HTTP transports must pass
    `session_factory`. It is deliberately not `session`: telemetry writes its id
    row, and sharing this session would move that write into the caller's
    transaction.
    """
    source = event.source
    webhook_payload = event.payload

    record = IntegrationEvent(
        org_id=org_id,
        source=source,
        provider_event_id=event.provider_event_id,
        event_key=event.event_key,
        payload=webhook_payload,
    )
    # Insert before doing any routing work, so a redelivery costs one failed
    # INSERT rather than a match pass. The savepoint is what keeps the
    # violation recoverable: without it the failure would poison the
    # transaction the caller still has to commit.
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        if event.provider_event_id is None:
            # The dedupe index is partial: with no id there is nothing on this
            # row that can conflict, so whatever failed is not a redelivery.
            # Reading it as one would drop a genuine event without a trace.
            raise
        logger.info(
            "Dropping duplicate %s event %s for org=%s",
            source,
            event.provider_event_id,
            org_id,
        )
        return AcceptResult(matched=0, run_ids=[], duplicate=True)

    automations = await get_event_automations(org_id, source, session)
    matched_automations = []

    for automation, trigger in automations:
        if matches_trigger(trigger, source, event.event_key, webhook_payload):
            matched_automations.append(automation)

    record.matched_count = len(matched_automations)

    logger.info(
        "Event matched %d/%d automations for org=%s",
        len(matched_automations),
        len(automations),
        org_id,
    )
    await capture_automation_event(
        "automation_event_matched",
        request=request,
        session_factory=session_factory,
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
            session_factory=session_factory,
            automation=automation,
            run=run,
            properties=run_properties,
        )
        await capture_automation_event(
            "automation_run_created",
            request=request,
            session_factory=session_factory,
            automation=automation,
            run=run,
            properties=run_properties,
        )

    await session.commit()

    return AcceptResult(
        matched=len(matched_automations),
        run_ids=run_ids,
    )
