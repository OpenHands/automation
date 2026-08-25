"""Mapping an external subject to a conversation the agent server owns.

The service stores only the correspondence. Ordering is fixed by one fact: a
conversation id arrives on the run's completion callback, so a mapping is
written when a run is created and filled in by `attach_run_conversation()`.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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

# Matches ExternalConversation.subject_key. Truncating would merge two subjects.
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
    """The key this event's siblings are grouped by, or None for a plain run.

    A trigger's `subject_key_expr` wins over the provider's extractor.
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
        # A dict is not an identity; `true` would collapse every event onto one.
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

    The lock serialises concurrent events for one subject. SQLite has no row
    locks and runs single-process, so it simply reads.
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

    Returns the conversation id when the turn landed, None when the caller
    should create a run: no mapping, no conversation recorded yet, or one that
    can no longer be reached. Holds the mapping's row lock until the caller
    commits.
    """
    mapping = await _lock_mapping(session, org_id, source, subject_key, automation_id)
    if mapping is None:
        return None

    conversation_id = mapping.conversation_id
    if not conversation_id or mapping.run_id is None:
        # A run is in flight and has not reported its conversation. Nothing to
        # continue, and this design has no queue to hold the event in.
        return None

    # The automation is eager-loaded: minting a cloud API key reads
    # `run.automation`, and a lazy load there raises MissingGreenlet, which
    # `send_conversation_turn` would swallow into a silent fallback.
    run = (
        (
            await session.execute(
                select(AutomationRun)
                .where(AutomationRun.id == mapping.run_id)
                .options(selectinload(AutomationRun.automation))
            )
        )
        .scalars()
        .first()
    )
    if run is None:
        return None

    delivered = await send_conversation_turn(
        run,
        conversation_id,
        compose_turn(source, event_key, event_payload),
    )
    if not delivered:
        # Forget it, so the caller's new run takes over the subject rather than
        # leaving the mapping pinned to a dead sandbox.
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

    Losing the insert race means a concurrent event claimed the subject first;
    this run then just does not own it, and nothing is dropped.
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
    # Savepoint: the caller still has runs to commit in this transaction.
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

    Returns whether this run owns one. The caller uses it to keep the sandbox:
    a conversation inside a deleted sandbox cannot be continued.
    """
    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(ExternalConversation)
        .where(ExternalConversation.run_id == run_id)
        .values(conversation_id=conversation_id)
    )
    return bool(result.rowcount)
