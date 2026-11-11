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
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.conversations import (
    CONTINUE_CONVERSATION,
    continue_conversation,
    resolve_subject_key,
    resolve_turn_text,
)
from openhands.automation.models import Automation, AutomationRun, IntegrationEvent
from openhands.automation.observability import (
    add_event,
    automation_attributes,
    current_span_context,
    span,
)
from openhands.automation.observability_associations import (
    evaluate_observability_associations,
)
from openhands.automation.schemas import EventTrigger
from openhands.automation.subjects import service_account_id_of
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.trigger_matcher import matches_trigger
from openhands.automation.utils.webhook import (
    create_automation_run,
    get_event_automations,
)


logger = logging.getLogger("automation.ingest")

__all__ = ["AcceptResult", "AcceptedEvent", "accept_event"]


@dataclass(frozen=True, slots=True)
class AcceptedEvent:
    """An authenticated, interpreted event, ready to be routed."""

    source: str
    event_key: str
    # Raw provider payload; JMESPath trigger filters run on this.
    payload: dict[str, Any] = field(default_factory=dict)
    provider_event_id: str | None = None
    occurred_at: datetime | None = None
    # When set, persisted as the run's event_payload in place of `payload`.
    parsed_event: BaseModel | None = None


@dataclass(frozen=True, slots=True)
class AcceptResult:
    """The outcome of routing an accepted event."""

    matched: int
    run_ids: list[str]
    duplicate: bool = False
    # Conversations continued instead of starting a run. A matched automation
    # contributes to exactly one of these two lists.
    conversation_ids: list[str] = field(default_factory=list)
    # Deadline-bound transports own delivery after their response is sent.
    # Never emitted for a transaction that failed to commit.
    telemetry: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list, repr=False, compare=False
    )

    async def capture_telemetry(
        self,
        *,
        request: Request | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        pending = list(self.telemetry)
        self.telemetry.clear()
        for event, kwargs in pending:
            await capture_automation_event(
                event, request=request, session_factory=session_factory, **kwargs
            )


def _telemetry_snapshot(
    model: Automation | AutomationRun,
) -> Automation | AutomationRun:
    """Keep capture-time values usable after commit and session closure.

    Copy only loaded columns to a transient model. Telemetry must not retain
    live ORM state, trigger lazy reads, or observe a later run-state change.
    """
    return type(model)(
        **{
            column.key: model.__dict__[column.key]
            for column in inspect(type(model)).column_attrs
            if column.key in model.__dict__
        }
    )


class ContinuationNotAllowed(ValueError):
    """A deadline-bound delivery cannot safely use this routing snapshot."""


async def accept_event(
    org_id: uuid.UUID,
    event: AcceptedEvent,
    session: AsyncSession,
    *,
    request: Request | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    allow_conversation_continuation: bool = True,
    defer_telemetry: bool = False,
) -> AcceptResult:
    """Route an already-authenticated event to the org's matching automations.

    Records the event and creates the runs in one transaction, deduplicating on
    `event.provider_event_id` when the transport supplies one. Commits before
    returning. Product telemetry runs only after commit. A transport selecting
    `defer_telemetry` must call the result's `capture_telemetry` after sending
    its response; otherwise capture completes here, after releasing DB locks.

    A matched automation whose trigger sets `destination` to
    `continue_conversation` first tries to deliver the event as another turn on
    its subject's conversation, creating no run. A subject whose run is still
    queued has the turn folded into that run instead, so a burst cannot leave
    one subject with two runs. Anything else -- no subject, no run holding it,
    one whose sandbox has gone -- falls back to creating a run.

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

    # Fetch once and route from this same snapshot. In deadline-bound mode,
    # reject the whole delivery before even persisting an event. Checking in
    # the HTTP layer alone would race a concurrent automation configuration
    # change between the check and this function's routing query.
    automations = None
    if not allow_conversation_continuation:
        automations = await get_event_automations(org_id, source, session)
        if any(
            trigger.destination == CONTINUE_CONVERSATION for _, trigger in automations
        ):
            raise ContinuationNotAllowed(
                "Deadline-bound sources cannot continue conversations"
            )

    # Use a targeted conflict clause rather than a SAVEPOINT. With SQLite's
    # legacy transaction control a first SAVEPOINT can commit its INSERT on
    # release, leaving an event behind if the outer run transaction fails.
    # ON CONFLICT keeps event + runs in the same transaction on both backends,
    # and only the delivery-ID uniqueness constraint is treated as a duplicate.
    insert = (
        sqlite.insert
        if session.get_bind().dialect.name == "sqlite"
        else postgresql.insert
    )
    statement = (
        insert(IntegrationEvent)
        .values(
            org_id=org_id,
            source=source,
            provider_event_id=event.provider_event_id,
            event_key=event.event_key,
            payload=webhook_payload,
        )
        .on_conflict_do_nothing(
            index_elements=[
                IntegrationEvent.org_id,
                IntegrationEvent.source,
                IntegrationEvent.provider_event_id,
            ],
            index_where=IntegrationEvent.provider_event_id.is_not(None),
        )
        .returning(IntegrationEvent)
    )
    record = (await session.scalars(statement)).one_or_none()
    if record is None:
        logger.info(
            "Dropping duplicate %s event %s for org=%s",
            source,
            event.provider_event_id,
            org_id,
        )
        add_event(
            "automation.event.duplicate",
            {
                "automation.org_id": str(org_id),
                "automation.event.source": source,
                "automation.event.key": event.event_key,
                "automation.event.provider_event_id": event.provider_event_id,
            },
        )
        return AcceptResult(matched=0, run_ids=[], duplicate=True)

    if automations is None:
        automations = await get_event_automations(org_id, source, session)
    matched: list[tuple[Automation, EventTrigger]] = []
    for automation, trigger in automations:
        matched_trigger = False
        with span(
            "automation.match.evaluate",
            automation_attributes(
                automation,
                **{
                    "automation.event.source": source,
                    "automation.event.key": event.event_key,
                    "automation.event.provider_event_id": event.provider_event_id,
                    "automation.match.filter_expression": trigger.filter,
                    "automation.match.destination": trigger.destination,
                },
            ),
        ):
            matched_trigger = matches_trigger(
                trigger, source, event.event_key, webhook_payload
            )
            add_event(
                "automation.match.evaluated",
                automation_attributes(
                    automation,
                    **{
                        "automation.event.source": source,
                        "automation.event.key": event.event_key,
                        "automation.match.matched": matched_trigger,
                        "automation.match.decision": "matched"
                        if matched_trigger
                        else "not_matched",
                    },
                ),
            )
        if matched_trigger:
            matched.append((automation, trigger))

    record.matched_count = len(matched)
    add_event(
        "automation.event.matched",
        {
            "automation.org_id": str(org_id),
            "automation.event.source": source,
            "automation.event.key": event.event_key,
            "automation.event.provider_event_id": event.provider_event_id,
            "automation.event.candidate_count": len(automations),
            "automation.event.matched_count": len(matched),
        },
    )

    logger.info(
        "Event matched %d/%d automations for org=%s",
        len(matched),
        len(automations),
        org_id,
    )
    pending_telemetry: list[tuple[str, dict[str, Any]]] = []
    pending_telemetry.append(
        (
            "automation_event_matched",
            {
                "properties": {
                    "event_source": source,
                    "event_key": event.event_key,
                    "org_id": str(org_id),
                    "candidate_count": len(automations),
                    "matched_count": len(matched),
                }
            },
        )
    )

    event_parent_span_context = current_span_context()

    # Typed events (GitHub) keep their model shape; others store the payload.
    event_payload = (
        event.parsed_event.model_dump(mode="json")
        if isinstance(event.parsed_event, BaseModel)
        else webhook_payload
    )

    run_ids: list[str] = []
    conversation_ids: list[str] = []
    for automation, trigger in matched:
        subject_key = (
            resolve_subject_key(trigger, webhook_payload)
            if trigger.destination == CONTINUE_CONVERSATION
            else None
        )

        if subject_key is not None:
            outcome = await continue_conversation(
                session,
                org_id=org_id,
                source=source,
                subject_key=subject_key,
                automation_id=automation.id,
                service_account_id=service_account_id_of(automation),
                event_key=event.event_key,
                event_payload=event_payload,
                turn_text=resolve_turn_text(trigger, webhook_payload),
                wake_agent=trigger.wake_agent,
            )
            if not outcome.needs_run:
                assert outcome.conversation_id is not None
                conversation_ids.append(outcome.conversation_id)
                add_event(
                    "automation.route.conversation_continued",
                    automation_attributes(
                        automation,
                        **{
                            "automation.event.source": source,
                            "automation.event.key": event.event_key,
                            "automation.subject.key": subject_key,
                            "automation.conversation_id": outcome.conversation_id,
                            "openhands.conversation_id": outcome.conversation_id,
                            "automation.route.coalesced": outcome.coalesced,
                        },
                    ),
                )
                pending_telemetry.append(
                    (
                        "automation_conversation_continued",
                        {
                            "automation": _telemetry_snapshot(automation),
                            "properties": {
                                "event_source": source,
                                "event_key": event.event_key,
                                "org_id": str(org_id),
                                # Folded into a run that had not started, rather than
                                # posted to a live agent server.
                                "coalesced": outcome.coalesced,
                            },
                        },
                    )
                )
                continue

        associations = evaluate_observability_associations(
            automation.observability_associations,
            webhook_payload,
        )

        # How a later event on this subject finds this run's sandbox.
        run = await create_automation_run(
            automation,
            session,
            event_payload=event_payload,
            subject_key=subject_key,
            trigger_event_id=record.id,
            observability_parent_span_context=event_parent_span_context,
            observability_associations=associations or None,
        )
        run_ids.append(str(run.id))
        run_created_attributes = {
            **associations,
            **automation_attributes(
                automation,
                run,
                **{
                    "automation.event.source": source,
                    "automation.event.key": event.event_key,
                    "automation.event.provider_event_id": event.provider_event_id,
                    "automation.subject.key": subject_key,
                },
            ),
        }
        with span("automation.route.run_created", run_created_attributes):
            add_event("automation.route.run_created", run_created_attributes)
        run_properties = {
            "trigger_source": "event",
            "event_source": source,
            "event_key": event.event_key,
        }
        pending_telemetry.append(
            (
                "automation_run_created",
                {
                    "automation": _telemetry_snapshot(automation),
                    "run": _telemetry_snapshot(run),
                    "properties": run_properties,
                },
            )
        )

    await session.commit()

    result = AcceptResult(
        matched=len(matched),
        run_ids=run_ids,
        conversation_ids=conversation_ids,
        telemetry=pending_telemetry,
    )
    if not defer_telemetry:
        await result.capture_telemetry(request=request, session_factory=session_factory)
    return result
