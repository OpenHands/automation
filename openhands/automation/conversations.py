"""Routing an event to the conversation its subject already has.

No mapping table: the id is derived (`subjects.conversation_id_for`). Only the
*sandbox* has to be looked up, via `AutomationRun.subject_key` -- a turn sent
there costs no run at all.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openhands.automation.db import using_sqlite
from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
)
from openhands.automation.models import AutomationRun
from openhands.automation.providers import get_provider
from openhands.automation.schemas import EventTrigger
from openhands.automation.subjects import EventSubject, conversation_id_for
from openhands.automation.utils.conversation_turn import (
    compose_turn,
    send_conversation_turn,
)


logger = logging.getLogger("automation.conversations")

CONTINUE_CONVERSATION = "continue_conversation"

# Matches AutomationRun.subject_key. Truncating would merge two subjects.
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


async def _lock_subject_run(
    session: AsyncSession,
    automation_id: uuid.UUID,
    subject_key: str,
) -> AutomationRun | None:
    """The most recent run holding this subject's conversation, locked.

    The lock serialises concurrent events for one subject; SQLite has none and
    runs single-process, so it simply reads. The automation is eager-loaded
    because minting a cloud API key reads `run.automation`, and a lazy load
    there raises MissingGreenlet -- which `send_conversation_turn` would
    swallow into a silent fallback.
    """
    stmt = (
        select(AutomationRun)
        .where(
            AutomationRun.automation_id == automation_id,
            AutomationRun.subject_key == subject_key,
            AutomationRun.sandbox_id.isnot(None),
        )
        .order_by(AutomationRun.created_at.desc())
        .limit(1)
        .options(selectinload(AutomationRun.automation))
    )
    if not using_sqlite():
        stmt = stmt.with_for_update(of=AutomationRun)
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
    should create a run: nothing has run for this subject yet, or its sandbox
    is gone. Holds the run's row lock until the caller commits.

    The id is known before the first run finishes, so an event arriving
    mid-run continues that conversation instead of racing a second run.
    """
    run = await _lock_subject_run(session, automation_id, subject_key)
    if run is None:
        return None

    conversation_id = conversation_id_for(org_id, automation_id, source, subject_key)
    delivered = await send_conversation_turn(
        run,
        conversation_id,
        compose_turn(source, event_key, event_payload),
    )
    if not delivered:
        # Sandbox reaped. Clear it so later events do not pay the timeout to
        # rediscover it; the caller's new run takes the subject over.
        run.subject_key = None
        return None

    return conversation_id
