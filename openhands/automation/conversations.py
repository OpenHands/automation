"""Routing an event to the conversation its subject already has.

Two things live here: deriving a subject key from an event, and the mapping
from that key to a conversation the agent server manages. The service stores
the correspondence and nothing else -- no session states, no pending-event
queue, no sandbox restart semantics. A conversation's lifecycle stays where the
conversation does.

The order of operations is fixed by one fact: the service does not learn a
conversation id until the run that created it completes and posts one on the
completion callback. So a mapping is written when a run is created, pointing at
that run, and filled in later by `attach_run_conversation()`.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.db import using_sqlite
from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
)
from openhands.automation.models import AutomationRun, ExternalConversation
from openhands.automation.providers import get_provider
from openhands.automation.schemas import EventTrigger
from openhands.automation.subjects import EventSubject
from openhands.automation.utils.conversation_turn import (
    compose_turn,
    send_conversation_turn,
)
from openhands.automation.utils.time import utcnow


logger = logging.getLogger("automation.conversations")

CONTINUE_CONVERSATION = "continue_conversation"

# Matches ExternalConversation.subject_key. A longer key is a broken extractor,
# not a long thread, and truncating one would silently merge two subjects.
MAX_SUBJECT_KEY_LENGTH = 500


def event_subject(source: str, payload: dict[str, Any]) -> EventSubject | None:
    """The subject a provider derives from a payload, if it has an extractor."""
    provider = get_provider(source)
    if provider is None or provider.subject is None:
        return None
    try:
        return provider.subject(payload)
    except Exception as exc:
        logger.warning("Subject extractor for %s failed: %s", source, exc)
        return None


def resolve_subject_key(
    trigger: EventTrigger,
    payload: dict[str, Any],
    subject: EventSubject | None,
) -> str | None:
    """The key `continue_conversation` groups this event's siblings by.

    A trigger's own `subject_key_expr` wins over the provider's extractor: the
    provider describes its source in general, the trigger knows what this
    particular automation considers one conversation.

    None means this event has no subject -- a GitHub `push`, a custom webhook
    with no expression configured -- and the caller starts a run, which is what
    it would have done anyway.
    """
    if trigger.subject_key_expr:
        return _key_from_expression(trigger.subject_key_expr, payload)
    return subject.key if subject is not None else None


def _key_from_expression(expression: str, payload: dict[str, Any]) -> str | None:
    try:
        value = evaluate_expression(expression, payload)
    except FilterEvaluationError as exc:
        logger.warning("subject_key_expr %r failed: %s", expression, exc)
        return None

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        # A dict or a list is not an identity. Booleans are excluded for the
        # same reason: `true` would collapse every event onto one subject.
        logger.warning(
            "subject_key_expr %r yielded a non-scalar (%s); ignoring it",
            expression,
            type(value).__name__,
        )
        return None

    key = str(value).strip()
    if not key or len(key) > MAX_SUBJECT_KEY_LENGTH:
        logger.warning(
            "subject_key_expr %r yielded an unusable key of length %d",
            expression,
            len(key),
        )
        return None
    return key


async def _lock_mapping(
    session: AsyncSession,
    org_id: uuid.UUID,
    source: str,
    subject_key: str,
    automation_id: uuid.UUID,
) -> ExternalConversation | None:
    """Read the subject's mapping, locking it for the rest of the transaction.

    The lock is the answer to concurrency: a second event for the same Slack
    thread blocks here until the first has committed, so it sees the first's
    decision rather than racing it. SQLite has no row locks and runs
    single-process, so it simply reads.
    """
    stmt = select(ExternalConversation).where(
        ExternalConversation.org_id == org_id,
        ExternalConversation.source == source,
        ExternalConversation.subject_key == subject_key,
        ExternalConversation.automation_id == automation_id,
    )
    if not using_sqlite():
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalars().first()


async def continue_conversation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source: str,
    subject_key: str,
    automation_id: uuid.UUID,
    event_key: str,
    event_payload: dict[str, Any] | None,
) -> str | None:
    """Deliver this event as another turn on the subject's conversation.

    Returns the conversation id when the agent server accepted the turn, and
    None when the caller should create a run instead -- no mapping yet, no
    conversation recorded against it yet, or a conversation that can no longer
    be reached.

    Takes the mapping's row lock, which is held until the caller commits.
    """
    mapping = await _lock_mapping(session, org_id, source, subject_key, automation_id)
    if mapping is None:
        return None

    conversation_id = mapping.conversation_id
    if not conversation_id or mapping.run_id is None:
        # A run for this subject is in flight and has not reported its
        # conversation yet. There is nothing to continue, so this event gets a
        # run of its own; the alternative is a queue, which this design does
        # not have.
        return None

    run = await session.get(AutomationRun, mapping.run_id)
    if run is None:
        return None

    delivered = await send_conversation_turn(
        run,
        conversation_id,
        compose_turn(source, event_key, event_payload),
    )
    if not delivered:
        # Unreachable: the sandbox was reaped, or the conversation is gone.
        # Forget it, so the run the caller is about to create takes over the
        # subject instead of the mapping staying pinned to a dead sandbox.
        mapping.conversation_id = None
        mapping.run_id = None
        mapping.last_event_at = utcnow()
        return None

    mapping.last_event_at = utcnow()
    return conversation_id


async def record_subject_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source: str,
    subject_key: str,
    automation_id: uuid.UUID,
    run: AutomationRun,
) -> None:
    """Point the subject's mapping at the run that will own its conversation.

    Best effort by design. Losing the race to insert means a concurrent event
    for the same subject claimed it first; that event's run becomes the
    subject's conversation and this one is an ordinary run. Nothing is dropped.
    """
    now = utcnow()
    mapping = await _lock_mapping(session, org_id, source, subject_key, automation_id)
    if mapping is not None:
        mapping.run_id = run.id
        mapping.conversation_id = None
        mapping.last_event_at = now
        return

    mapping = ExternalConversation(
        org_id=org_id,
        source=source,
        subject_key=subject_key,
        automation_id=automation_id,
        run_id=run.id,
        last_event_at=now,
    )
    # A savepoint, for the same reason the event insert has one: the unique
    # violation has to stay recoverable, because the caller still has runs to
    # commit in this transaction.
    try:
        async with session.begin_nested():
            session.add(mapping)
            await session.flush()
    except IntegrityError:
        logger.info(
            "Subject %s/%s was claimed concurrently; run %s will not own it",
            source,
            subject_key,
            run.id,
        )


async def attach_run_conversation(
    session: AsyncSession,
    run_id: uuid.UUID,
    conversation_id: str,
) -> bool:
    """Record the conversation a run created, against the subject it was for.

    Called from the completion callback, which is the first moment the service
    knows the id.

    Returns whether this run owns a subject's conversation. False is the
    ordinary case -- most runs are not about an external subject -- and the
    caller uses it to decide whether the sandbox may be deleted: a conversation
    inside a deleted sandbox cannot be continued, which would make
    `continue_conversation` a no-op in cloud mode.
    """
    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(ExternalConversation)
        .where(ExternalConversation.run_id == run_id)
        .values(conversation_id=conversation_id)
    )
    return bool(result.rowcount)
